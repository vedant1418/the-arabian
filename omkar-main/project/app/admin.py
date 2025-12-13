from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Resort, Booking, Payment, Guest, GalleryImage ,CustomUser, PasswordResetOTP


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['guest_name', 'resort_name', 'guest_phone', 'check_in', 'check_out', 'booking_status']
    list_filter = ['booking_status', 'check_in']
    search_fields = ['guest_name', 'guest_email', 'guest_phone']

    def resort_name(self, obj):
        return obj.resort.name
    resort_name.short_description = "Resort"


@admin.register(Resort)
class ResortAdmin(admin.ModelAdmin):
    list_display = ['name', 'location', 'price_per_guest']
    search_fields = ['name', 'location']


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['booking', 'payment_id', 'payment_method', 'payment_time', 'amount_paid']
    search_fields = ['payment_id']
    list_filter = ['payment_time', 'payment_method']


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'booking', 'age']
    search_fields = ['full_name']
    list_filter = ['age']


@admin.register(GalleryImage)
class GalleryImageAdmin(admin.ModelAdmin):
    list_display = ['title', 'uploaded_at']
    search_fields = ['title']
    list_filter = ['uploaded_at']

class CustomUserAdmin(UserAdmin):
    model = CustomUser
    list_display = ("email", "phone", "is_staff", "is_active")
    list_filter = ("is_staff", "is_active")

    ordering = ("email",)   # FIXED (use email, not username)

    fieldsets = (
        (None, {"fields": ("email", "phone", "password")}),
        ("Permissions", {"fields": ("is_staff", "is_active", "groups", "user_permissions")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "phone", "password1", "password2", "is_staff", "is_active"),
        }),
    )

# admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(PasswordResetOTP)
