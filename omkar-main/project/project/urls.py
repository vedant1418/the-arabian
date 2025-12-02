from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from app import views  # change "app" to your app name


urlpatterns = [
     path("dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path('admin/', admin.site.urls),
    path('', include('app.urls')),  # Replace 'app' with your actual app name if different
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
