from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from callouts.models import Announcement
from callouts.permissions import IsActiveLeaderInOwnLocal
from callouts.serializers import AnnouncementSerializer


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    return Response({'status': 'ok'})


@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    email = request.data.get('email')
    password = request.data.get('password')

    if not email or not password:
        return Response(
            {'detail': 'Email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    member = authenticate(request, username=email, password=password)
    if member is None:
        return Response(
            {'detail': 'Invalid email or password.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    token, _ = Token.objects.get_or_create(user=member)
    return Response(
        {
            'token': token.key,
            'token_type': 'Token',
            'member': {
                'id': str(member.id),
                'local_id': str(member.local_id),
                'full_name': member.full_name,
                'email': member.email,
                'classification': member.classification,
                'status': member.status,
                'role': member.role,
            },
        }
    )


@api_view(['POST'])
@permission_classes([IsActiveLeaderInOwnLocal])
def announcements(request):
    serializer = AnnouncementSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    announcement = serializer.save(
        local=request.user.local,
        created_by=request.user,
        status=Announcement.Status.DRAFT,
    )
    return Response(
        AnnouncementSerializer(announcement).data,
        status=status.HTTP_201_CREATED,
    )
