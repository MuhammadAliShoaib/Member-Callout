from django.core.exceptions import PermissionDenied


def request_local_id(request):
    user = getattr(request, 'user', None)
    local_id = getattr(user, 'local_id', None)

    if not local_id:
        raise PermissionDenied('Authenticated member local is required.')

    return local_id


def for_request_local(queryset, request):
    return queryset.filter(local_id=request_local_id(request))


def set_request_local(instance, request):
    instance.local_id = request_local_id(request)
    return instance


def validate_request_local(instance, request):
    if getattr(instance, 'local_id', None) != request_local_id(request):
        raise PermissionDenied('Object does not belong to your local.')

    return instance
