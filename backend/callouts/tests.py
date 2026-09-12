from types import SimpleNamespace

from django.test import SimpleTestCase

from callouts.models import Local, Member
from callouts.permissions import IsActiveLeaderInOwnLocal


class ActiveLeaderPermissionTests(SimpleTestCase):
    def setUp(self):
        self.local_27 = Local(name='Local 27')
        self.local_99 = Local(name='Local 99')
        self.permission = IsActiveLeaderInOwnLocal()

    def test_local_27_leader_cannot_access_local_99_data_and_vice_versa(self):
        leader_27 = self.member(self.local_27, Member.Role.LEADER)
        leader_99 = self.member(self.local_99, Member.Role.LEADER)

        self.assertTrue(
            self.permission.has_object_permission(
                self.request(leader_27),
                None,
                self.local_data(self.local_27),
            )
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(leader_27),
                None,
                self.local_data(self.local_99),
            )
        )
        self.assertTrue(
            self.permission.has_object_permission(
                self.request(leader_99),
                None,
                self.local_data(self.local_99),
            )
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(leader_99),
                None,
                self.local_data(self.local_27),
            )
        )

    def test_members_cannot_access_local_27_or_local_99_data(self):
        member_27 = self.member(self.local_27, Member.Role.MEMBER)
        member_99 = self.member(self.local_99, Member.Role.MEMBER)

        for user in (member_27, member_99):
            request = self.request(user)
            self.assertFalse(self.permission.has_permission(request, None))
            self.assertFalse(
                self.permission.has_object_permission(
                    request,
                    None,
                    self.local_data(self.local_27),
                )
            )
            self.assertFalse(
                self.permission.has_object_permission(
                    request,
                    None,
                    self.local_data(self.local_99),
                )
            )

    def test_normal_members_cannot_perform_leader_operations(self):
        member_27 = self.member(self.local_27, Member.Role.MEMBER)
        member_99 = self.member(self.local_99, Member.Role.MEMBER)

        self.assertFalse(
            self.permission.has_permission(self.request(member_27), None)
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(member_27),
                None,
                self.local_data(self.local_27),
            )
        )
        self.assertFalse(
            self.permission.has_permission(self.request(member_99), None)
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(member_99),
                None,
                self.local_data(self.local_99),
            )
        )

    def member(self, local, role):
        return Member(
            local=local,
            full_name=f'{local.name} {role.label}',
            email=f'{local.name.lower().replace(" ", "")}.{role}@example.com',
            classification='apprentice',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def request(self, user):
        return SimpleNamespace(user=user)

    def local_data(self, local):
        return SimpleNamespace(local_id=local.id)
