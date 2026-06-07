from django.urls import path
from .views import ChatAPIview, chat_interface

urlpatterns = [
    path('', chat_interface, name='chat_interface'),
    path('api/chat/', ChatAPIview.as_view(), name='chat_api'),
]