from __future__ import annotations

from sqlmodel import Session, select

from app.models import Group, OnlinePolicy, Subject


class OnlinePolicyService:
    def get_target_for_group(self, session: Session, group: Group) -> int:
        override = session.exec(
            select(OnlinePolicy).where(
                OnlinePolicy.group_id == group.id,
                OnlinePolicy.subject_id.is_(None),
                OnlinePolicy.is_active.is_(True),
            )
        ).first()
        if override:
            return override.target_online_lessons_per_week
        course_policy = session.exec(
            select(OnlinePolicy).where(
                OnlinePolicy.course == (group.year or group.course),
                OnlinePolicy.group_id.is_(None),
                OnlinePolicy.subject_id.is_(None),
                OnlinePolicy.is_active.is_(True),
            )
        ).first()
        if course_policy:
            return course_policy.target_online_lessons_per_week
        return 0

    def is_subject_allowed_online(self, session: Session, group: Group, subject: Subject) -> bool:
        if not subject.can_be_online:
            return False
        subject_override = session.exec(
            select(OnlinePolicy).where(
                OnlinePolicy.group_id == group.id,
                OnlinePolicy.subject_id == subject.id,
                OnlinePolicy.is_active.is_(True),
            )
        ).first()
        if subject_override:
            return subject_override.allow_online
        course_subject_policy = session.exec(
            select(OnlinePolicy).where(
                OnlinePolicy.course == (group.year or group.course),
                OnlinePolicy.subject_id == subject.id,
                OnlinePolicy.is_active.is_(True),
            )
        ).first()
        if course_subject_policy:
            return course_subject_policy.allow_online
        if (group.year or group.course) == 1:
            return False
        return True

    def upsert_course_target(self, session: Session, course: int, target: int, allow_online: bool = True, note: str = "") -> OnlinePolicy:
        policy = session.exec(
            select(OnlinePolicy).where(
                OnlinePolicy.course == course,
                OnlinePolicy.group_id.is_(None),
                OnlinePolicy.subject_id.is_(None),
            )
        ).first()
        if policy is None:
            policy = OnlinePolicy(course=course)
        policy.target_online_lessons_per_week = target
        policy.allow_online = allow_online
        policy.is_active = True
        policy.note = note
        session.add(policy)
        session.commit()
        session.refresh(policy)
        return policy

    def upsert_group_target(self, session: Session, group_id: int, target: int, note: str = "") -> OnlinePolicy:
        policy = session.exec(
            select(OnlinePolicy).where(
                OnlinePolicy.group_id == group_id,
                OnlinePolicy.subject_id.is_(None),
            )
        ).first()
        if policy is None:
            policy = OnlinePolicy(group_id=group_id)
        policy.target_online_lessons_per_week = target
        policy.allow_online = True
        policy.is_active = True
        policy.note = note
        session.add(policy)
        session.commit()
        session.refresh(policy)
        return policy

    def upsert_subject_policy(
        self,
        session: Session,
        subject_id: int,
        allow_online: bool,
        course: int | None = None,
        group_id: int | None = None,
        note: str = "",
    ) -> OnlinePolicy:
        query = select(OnlinePolicy).where(OnlinePolicy.subject_id == subject_id)
        if course is None:
            query = query.where(OnlinePolicy.course.is_(None))
        else:
            query = query.where(OnlinePolicy.course == course)
        if group_id is None:
            query = query.where(OnlinePolicy.group_id.is_(None))
        else:
            query = query.where(OnlinePolicy.group_id == group_id)
        policy = session.exec(query).first()
        if policy is None:
            policy = OnlinePolicy(subject_id=subject_id, course=course, group_id=group_id)
        policy.allow_online = allow_online
        policy.is_active = True
        policy.note = note
        session.add(policy)
        session.commit()
        session.refresh(policy)
        return policy
