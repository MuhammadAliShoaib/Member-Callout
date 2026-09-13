"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from callouts.views import (
    announcement_ai_draft,
    announcement_ai_regenerate,
    announcement_confirm,
    announcement_detail,
    announcement_send,
    announcement_stats,
    announcements,
    health,
    login,
    member_announcement_acknowledge,
    member_announcement_detail,
    member_announcements,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', health, name='health'),
    path('api/login/', login, name='login'),
    path('api/announcements/', announcements, name='announcements'),
    path('api/announcements/ai-draft/', announcement_ai_draft, name='announcement-ai-draft'),
    path('api/announcements/ai/regenerate/', announcement_ai_regenerate, name='announcement-ai-regenerate'),
    path('api/announcements/<uuid:announcement_id>/confirm/', announcement_confirm, name='announcement-confirm'),
    path('api/announcements/<uuid:announcement_id>/send/', announcement_send, name='announcement-send'),
    path('api/announcements/<uuid:announcement_id>/stats/', announcement_stats, name='announcement-stats'),
    path('api/announcements/<uuid:announcement_id>/', announcement_detail, name='announcement-detail'),
    path('api/member/announcements/', member_announcements, name='member-announcements'),
    path('api/member/announcements/<uuid:announcement_id>/', member_announcement_detail, name='member-announcement-detail'),
    path('api/member/announcements/<uuid:announcement_id>/acknowledge/', member_announcement_acknowledge, name='member-announcement-acknowledge'),
]
