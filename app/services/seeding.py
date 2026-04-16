from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, delete, select

from app.core.timetable import (
    DELIVERY_OFFLINE,
    PAIR_DEFINITIONS,
    SHIFT_AFTERNOON,
    SHIFT_MORNING,
    pair_end,
    pair_label,
    pair_shift,
    pair_start,
)
from app.models import (
    AcademicPeriod,
    AppSetting,
    CurriculumLoad,
    Department,
    Group,
    GroupSubjectTeacher,
    OnlinePolicy,
    Room,
    Subject,
    Teacher,
    TeacherSubject,
    Timeslot,
)
from app.services.importers.academic_calendar_pdf import AcademicCalendarPdfImporter
from app.services.importers.curriculum_xls import CurriculumXlsImporter
from app.services.online_slots import OnlineSlotService
from app.services.online_policy import OnlinePolicyService
from app.services.weekly_workload import WeeklyWorkloadService


GROUP_SHIFT_MAP = {
    "ETB-1124-1": SHIFT_MORNING,
    "DTP-2201": SHIFT_AFTERNOON,
    "ETB-2202": SHIFT_MORNING,
    "IS-2201": SHIFT_AFTERNOON,
}


class Seeder:
    def __init__(self) -> None:
        self.curriculum_importer = CurriculumXlsImporter()
        self.calendar_importer = AcademicCalendarPdfImporter()
        self.online_policy_service = OnlinePolicyService()
        self.online_slot_service = OnlineSlotService()
        self.weekly_workload_service = WeeklyWorkloadService()

    def seed(self, session: Session, curriculum_path: Path, calendar_path: Path, weekly_workload_path: Path | None = None) -> None:
        departments = self._ensure_departments(session)
        self._ensure_rooms(session)
        self._refresh_timeslots(session)
        teachers = self._ensure_teachers(session, departments)
        groups = self._ensure_groups(session, departments)

        self._seed_etb_group(session, groups["ETB-1124-1"], departments, teachers, curriculum_path, calendar_path)
        self._seed_demo_groups(session, groups, departments, teachers)

        self._upgrade_existing_groups(session)
        self._upgrade_existing_subjects(session)
        self._ensure_default_settings(session)
        self._ensure_online_policies(session)
        self.online_slot_service.ensure_defaults(session)
        if weekly_workload_path and weekly_workload_path.exists():
            self.weekly_workload_service.import_docx(
                session,
                weekly_workload_path,
                calendar_path=calendar_path,
                curriculum_path=curriculum_path,
            )
        session.commit()

    def _ensure_departments(self, session: Session) -> dict[str, Department]:
        specs = {
            "IT": "Информационные технологии",
            "GEN": "Общеобразовательные дисциплины",
            "HUM": "Гуманитарные дисциплины",
            "SPORT": "Физическая культура",
        }
        items: dict[str, Department] = {}
        for code, name in specs.items():
            item = session.exec(select(Department).where(Department.code == code)).first()
            if item is None:
                item = Department(code=code, name=name)
            else:
                item.name = name
            session.add(item)
            session.commit()
            session.refresh(item)
            items[code] = item
        return items

    def _ensure_rooms(self, session: Session) -> dict[str, Room]:
        specs = {
            "A-101": ("Аудитория A-101", 32, "standard"),
            "A-102": ("Аудитория A-102", 28, "standard"),
            "LAB-1": ("Компьютерный класс 1", 24, "computer_lab"),
            "LAB-2": ("Компьютерный класс 2", 24, "computer_lab"),
            "DESIGN": ("Лаборатория дизайна", 20, "design_lab"),
        }
        rooms: dict[str, Room] = {}
        for code, (name, capacity, room_type) in specs.items():
            room = session.exec(select(Room).where(Room.code == code)).first()
            if room is None:
                room = Room(code=code, name=name, capacity=capacity, room_type=room_type)
            else:
                room.name = name
                room.capacity = capacity
                room.room_type = room_type
            session.add(room)
            session.commit()
            session.refresh(room)
            rooms[code] = room
        return rooms

    def _refresh_timeslots(self, session: Session) -> None:
        session.exec(delete(Timeslot))
        session.commit()
        for pair_number in PAIR_DEFINITIONS:
            session.add(
                Timeslot(
                    day_of_week=0,
                    pair_number=pair_number,
                    shift=pair_shift(pair_number),
                    start_time=pair_start(pair_number),
                    end_time=pair_end(pair_number),
                    label=f"{pair_label(pair_number)} {pair_start(pair_number)}-{pair_end(pair_number)}",
                )
            )
        session.commit()

    def _ensure_teachers(self, session: Session, departments: dict[str, Department]) -> dict[str, Teacher]:
        specs = {
            "maksat": ("Maksat Nurpeisov", "M. Nurpeisov", "IT", 24),
            "aliya": ("Aliya Serik", "A. Serik", "IT", 22),
            "dana": ("Dana Kairatova", "D. Kairatova", "IT", 20),
            "aidos": ("Aidos Zhaparov", "A. Zhaparov", "IT", 22),
            "marzhan": ("Marzhan Ibragim", "M. Ibragim", "IT", 22),
            "saule": ("Saule Tolegenova", "S. Tolegenova", "HUM", 18),
            "aiman": ("Aiman Abdullaeva", "A. Abdullaeva", "SPORT", 16),
            "askar": ("Askar Beket", "A. Beket", "IT", 20),
            "gulnar": ("Gulnar Omarova", "G. Omarova", "GEN", 18),
            "erlan": ("Erlan Saparbayev", "E. Saparbayev", "IT", 20),
        }
        items: dict[str, Teacher] = {}
        for key, (full_name, short_name, department_code, max_pairs) in specs.items():
            teacher = session.exec(select(Teacher).where(Teacher.full_name == full_name)).first()
            if teacher is None:
                teacher = Teacher(
                    full_name=full_name,
                    short_name=short_name,
                    home_department_id=departments[department_code].id or 0,
                    editable_name=full_name,
                    max_weekly_pairs=max_pairs,
                )
            else:
                teacher.short_name = short_name
                teacher.editable_name = full_name
                teacher.max_weekly_pairs = max_pairs
            session.add(teacher)
            session.commit()
            session.refresh(teacher)
            items[key] = teacher
        return items

    def _ensure_groups(self, session: Session, departments: dict[str, Department]) -> dict[str, Group]:
        specs = {
            "ETB-1124-1": {"student_count": 25, "course": 2, "semester": 4},
            "DTP-2201": {"student_count": 22, "course": 2, "semester": 4},
            "ETB-2202": {"student_count": 24, "course": 2, "semester": 4},
            "IS-2201": {"student_count": 23, "course": 2, "semester": 4},
        }
        items: dict[str, Group] = {}
        for code, payload in specs.items():
            group = session.exec(select(Group).where(Group.code == code)).first()
            if group is None:
                group = Group(
                    code=code,
                    name=code,
                    home_department_id=departments["IT"].id or 0,
                    course=payload["course"],
                    year=payload["course"],
                    semester=payload["semester"],
                    student_count=payload["student_count"],
                    shift=GROUP_SHIFT_MAP[code],
                )
            else:
                group.name = code
                group.home_department_id = departments["IT"].id or 0
                group.course = payload["course"]
                group.year = payload["course"]
                group.semester = payload["semester"]
                group.student_count = payload["student_count"]
                group.shift = GROUP_SHIFT_MAP[code]
            session.add(group)
            session.commit()
            session.refresh(group)
            items[code] = group
        return items

    def _seed_etb_group(
        self,
        session: Session,
        group: Group,
        departments: dict[str, Department],
        teachers: dict[str, Teacher],
        curriculum_path: Path,
        calendar_path: Path,
    ) -> None:
        if not session.exec(select(AcademicPeriod).where(AcademicPeriod.group_id == group.id)).first():
            periods = self.calendar_importer.import_group_periods(calendar_path, group.code)
            for period in periods:
                session.add(
                    AcademicPeriod(
                        group_id=group.id or 0,
                        semester=period.semester,
                        week_number=period.week_number,
                        period_type=period.period_type,
                        is_schedulable=period.is_schedulable,
                    )
                )
            session.commit()

        loads = self.curriculum_importer.import_group_loads(curriculum_path, semesters=(3, 4))
        teacher_plan = {
            "Дене қасиеттерін дамыту және жетілдріу": ["aiman"],
            "Экономиканың базалық білімін және кәсіпкерлік негіздерін қолдану": ["saule"],
            "Веб-сайтты  ақпарттық және техникалық қолдау": ["maksat", "askar"],
            "Сайттың үздіксіз жұмысын қамтамасыз ету": ["maksat", "erlan"],
            "Мобильді қосымшаларды әзірлеу": ["aliya", "marzhan"],
            "Бұлтты технологиялар бойынша әзірмелер": ["dana", "erlan"],
            "Ақпараттық - коммуникациялық технологиялардағы бизнес- талдау": ["aidos", "askar"],
        }
        for imported in loads:
            subject_code = f"ETB-{imported.subject_code}"
            subject = session.exec(select(Subject).where(Subject.code == subject_code)).first()
            if subject is None:
                owner_code = "SPORT" if "Дене" in imported.subject_name else "HUM" if "Экономиканың" in imported.subject_name else "IT"
                subject = Subject(
                    code=subject_code,
                    name=imported.subject_name,
                    owner_department_id=departments[owner_code].id or 0,
                    lesson_type=imported.lesson_type,
                    requires_special_room=imported.requires_special_room,
                    can_be_online=imported.can_be_online,
                    default_delivery_mode=imported.default_delivery_mode,
                )
                session.add(subject)
                session.commit()
                session.refresh(subject)
            else:
                owner_code = "SPORT" if "Дене" in imported.subject_name else "HUM" if "Экономиканың" in imported.subject_name else "IT"
                subject.name = imported.subject_name
                subject.owner_department_id = departments[owner_code].id or 0
                subject.lesson_type = imported.lesson_type
                subject.requires_special_room = imported.requires_special_room
                subject.can_be_online = imported.can_be_online
                subject.default_delivery_mode = imported.default_delivery_mode
                session.add(subject)
                session.commit()
                session.refresh(subject)

            existing_load = session.exec(
                select(CurriculumLoad).where(
                    CurriculumLoad.group_id == group.id,
                    CurriculumLoad.subject_id == subject.id,
                    CurriculumLoad.semester == imported.semester,
                )
            ).first()
            if existing_load is None:
                study_weeks = session.exec(
                    select(AcademicPeriod).where(
                        AcademicPeriod.group_id == group.id,
                        AcademicPeriod.semester == imported.semester,
                        AcademicPeriod.is_schedulable.is_(True),
                    )
                ).all()
                session.add(
                    CurriculumLoad(
                        group_id=group.id or 0,
                        subject_id=subject.id or 0,
                        semester=imported.semester,
                        total_hours=imported.schedulable_hours,
                        study_weeks=len(study_weeks),
                        hours_per_week=round(imported.schedulable_hours / max(len(study_weeks), 1), 2),
                        pairs_per_week=round(imported.schedulable_hours / 2 / max(len(study_weeks), 1), 2),
                        lesson_type=imported.lesson_type,
                        delivery_mode=imported.default_delivery_mode,
                        raw_total_hours=imported.raw_total_hours,
                        practice_hours=imported.practice_hours,
                        source_code=imported.subject_code,
                        source_type="imported",
                        note="Импортировано из учебного плана.",
                    )
                )
                session.commit()

            teacher_keys = teacher_plan.get(imported.subject_name, ["maksat"])
            self._ensure_teacher_mappings(session, group.id or 0, subject.id or 0, teacher_keys, teachers)

    def _seed_demo_groups(
        self,
        session: Session,
        groups: dict[str, Group],
        departments: dict[str, Department],
        teachers: dict[str, Teacher],
    ) -> None:
        demo_calendars = {
            3: [(week, "study", True) for week in range(1, 17)] + [(17, "exam_week", False), (18, "vacation", False)],
            4: [(week, "study", True) for week in range(23, 39)] + [(39, "exam_week", False), (40, "vacation", False)],
        }
        demo_loads = {
            "DTP-2201": [
                ("Основы графического дизайна", "mixed", True, {3: 96, 4: 64}, ["aliya", "marzhan"], False),
                ("Взаимодействие человека и компьютера", "lecture", False, {3: 64, 4: 64}, ["gulnar", "saule"], True),
                ("Практика веб-верстки", "practice", True, {3: 128, 4: 96}, ["maksat", "erlan"], False),
            ],
            "ETB-2202": [
                ("Веб-разработка", "mixed", True, {3: 128, 4: 128}, ["maksat", "askar"], True),
                ("Основы баз данных", "mixed", True, {3: 96, 4: 96}, ["aidos", "erlan"], True),
                ("Профессиональный английский язык", "lecture", False, {3: 64, 4: 64}, ["gulnar"], True),
            ],
            "IS-2201": [
                ("Шаблоны программирования", "mixed", True, {3: 96, 4: 96}, ["erlan", "maksat"], True),
                ("Системный анализ", "lecture", False, {3: 64, 4: 96}, ["aidos"], True),
                ("Лаборатория мобильного UX", "practice", True, {3: 64, 4: 96}, ["aliya", "dana"], False),
            ],
        }
        for group_code, group in groups.items():
            if group_code == "ETB-1124-1":
                continue
            if not session.exec(select(AcademicPeriod).where(AcademicPeriod.group_id == group.id)).first():
                for semester, items in demo_calendars.items():
                    for week_number, period_type, is_schedulable in items:
                        session.add(
                            AcademicPeriod(
                                group_id=group.id or 0,
                                semester=semester,
                                week_number=week_number,
                                period_type=period_type,
                                is_schedulable=is_schedulable,
                            )
                        )
                session.commit()
            for index, (name, lesson_type, special_room, semester_hours, teacher_keys, can_be_online) in enumerate(demo_loads[group_code], start=1):
                code = f"{group_code}-SUB-{index}"
                subject = session.exec(select(Subject).where(Subject.code == code)).first()
                if subject is None:
                    subject = Subject(
                        code=code,
                        name=name,
                        owner_department_id=departments["IT"].id or 0,
                        lesson_type=lesson_type,
                        requires_special_room=special_room,
                        can_be_online=can_be_online,
                        default_delivery_mode=DELIVERY_OFFLINE,
                    )
                    session.add(subject)
                    session.commit()
                    session.refresh(subject)
                else:
                    subject.name = name
                    subject.owner_department_id = departments["IT"].id or 0
                    subject.lesson_type = lesson_type
                    subject.requires_special_room = special_room
                    subject.can_be_online = can_be_online
                    session.add(subject)
                    session.commit()
                    session.refresh(subject)
                self._ensure_teacher_mappings(session, group.id or 0, subject.id or 0, teacher_keys, teachers)
                for semester, hours in semester_hours.items():
                    existing_load = session.exec(
                        select(CurriculumLoad).where(
                            CurriculumLoad.group_id == group.id,
                            CurriculumLoad.subject_id == subject.id,
                            CurriculumLoad.semester == semester,
                        )
                    ).first()
                    if existing_load:
                        continue
                    weeks = len([item for item in demo_calendars[semester] if item[2]])
                    session.add(
                        CurriculumLoad(
                            group_id=group.id or 0,
                            subject_id=subject.id or 0,
                            semester=semester,
                            total_hours=hours,
                            study_weeks=weeks,
                            hours_per_week=round(hours / weeks, 2),
                            pairs_per_week=round(hours / 2 / weeks, 2),
                            lesson_type=lesson_type,
                            delivery_mode=DELIVERY_OFFLINE,
                            raw_total_hours=hours,
                            practice_hours=0,
                            source_code=subject.code,
                            source_type="demo",
                            note="Демонстрационная нагрузка.",
                        )
                    )
                session.commit()

    def _ensure_teacher_mappings(
        self,
        session: Session,
        group_id: int,
        subject_id: int,
        teacher_keys: list[str],
        teachers: dict[str, Teacher],
    ) -> None:
        for priority, teacher_key in enumerate(teacher_keys, start=1):
            teacher = teachers[teacher_key]
            if session.exec(
                select(TeacherSubject).where(
                    TeacherSubject.teacher_id == teacher.id,
                    TeacherSubject.subject_id == subject_id,
                )
            ).first() is None:
                session.add(
                    TeacherSubject(
                        teacher_id=teacher.id or 0,
                        subject_id=subject_id,
                        can_teach=True,
                        priority=priority,
                    )
                )
            if session.exec(
                select(GroupSubjectTeacher).where(
                    GroupSubjectTeacher.group_id == group_id,
                    GroupSubjectTeacher.subject_id == subject_id,
                    GroupSubjectTeacher.teacher_id == teacher.id,
                )
            ).first() is None:
                session.add(
                    GroupSubjectTeacher(
                        group_id=group_id,
                        subject_id=subject_id,
                        teacher_id=teacher.id or 0,
                        fixed=priority == 1,
                    )
                )
        session.commit()

    def _upgrade_existing_groups(self, session: Session) -> None:
        groups = session.exec(select(Group)).all()
        for group in groups:
            group.year = group.course
            if group.code in GROUP_SHIFT_MAP:
                group.shift = GROUP_SHIFT_MAP[group.code]
            elif not group.shift:
                group.shift = SHIFT_MORNING
            session.add(group)
        session.commit()

    def _upgrade_existing_subjects(self, session: Session) -> None:
        subjects = session.exec(select(Subject)).all()
        for subject in subjects:
            subject.can_be_online = subject.can_be_online or self._is_online_capable(subject.name, subject.lesson_type)
            if not subject.default_delivery_mode:
                subject.default_delivery_mode = DELIVERY_OFFLINE
            session.add(subject)
        session.commit()

    def _ensure_default_settings(self, session: Session) -> None:
        self._ensure_setting(session, "ai_provider", "dummy")
        self._ensure_setting(session, "gemini_api_key", "")
        self._ensure_setting(session, "ui_language", "ru")
        self._ensure_setting(session, "online_slot_1_label", "Онлайн-слот 1")
        self._ensure_setting(session, "online_slot_2_label", "Онлайн-слот 2")
        self._ensure_setting(session, "online_slot_3_label", "Онлайн-слот 3")

    def _ensure_online_policies(self, session: Session) -> None:
        self.online_policy_service.upsert_course_target(session, 1, 0, allow_online=True, note="Для 1 курса онлайн только для выбранных предметов.")
        self.online_policy_service.upsert_course_target(session, 2, 3, allow_online=True, note="По умолчанию 3 онлайн-занятия в неделю.")
        self.online_policy_service.upsert_course_target(session, 3, 3, allow_online=True, note="По умолчанию 3 онлайн-занятия в неделю.")
        self.online_policy_service.upsert_course_target(session, 4, 0, allow_online=True, note="Без обязательной цели по онлайн-занятиям.")

        first_year_subjects = session.exec(select(Subject).where(Subject.owner_department_id.is_not(None))).all()
        for subject in first_year_subjects:
            if self._is_first_year_general_subject(subject.name):
                self.online_policy_service.upsert_subject_policy(
                    session,
                    subject_id=subject.id or 0,
                    allow_online=True,
                    course=1,
                    note="Разрешено для 1 курса как общий предмет.",
                )

    @staticmethod
    def _ensure_setting(session: Session, key: str, value: str) -> None:
        setting = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        if setting is None:
            setting = AppSetting(key=key, value=value)
        else:
            setting.value = setting.value or value
        session.add(setting)
        session.commit()

    @staticmethod
    def _is_online_capable(name: str, lesson_type: str) -> bool:
        lowered = name.lower()
        keywords = (
            "эконом",
            "business",
            "analysis",
            "interaction",
            "english",
            "құқық",
            "мәдениет",
            "әлеумет",
            "бұлт",
            "резерв",
            "қауіпсіз",
            "professional",
            "patterns",
            "systems",
            "теория",
        )
        return lesson_type == "lecture" or any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _is_first_year_general_subject(name: str) -> bool:
        lowered = name.lower()
        keywords = ("english", "эконом", "құқық", "мәдениет", "этика", "professional english")
        return any(keyword in lowered for keyword in keywords)
