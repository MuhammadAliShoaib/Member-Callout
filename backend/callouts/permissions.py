from rest_framework.permissions import BasePermission

from callouts.models import Member
from callouts.tenant import request_local_id


class IsActiveLeaderInOwnLocal(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and user.status == Member.Status.ACTIVE
            and user.role == Member.Role.LEADER
            and user.local_id
        )

    def has_object_permission(self, request, view, obj):
        if not self.has_permission(request, view):
            return False

        object_local_id = getattr(obj, 'local_id', None)
        if object_local_id is None and hasattr(obj, 'local'):
            object_local_id = getattr(obj.local, 'id', None)

        return object_local_id == request_local_id(request)
