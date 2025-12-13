from django.db import models
# from django.contrib.auth.models import User
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils import timezone
from datetime import timedelta
from django.conf import settings

class Resort(models.Model):
    name = models.CharField(max_length=100)
    location = models.CharField(max_length=255)
    description = models.TextField()
    amenities = models.TextField(blank=True)
    check_in_time = models.CharField(max_length=20, default="12:00 PM")
    check_out_time = models.CharField(max_length=20, default="10:00 AM")
    highlights = models.TextField(blank=True)

    price_per_guest = models.DecimalField(max_digits=8, decimal_places=2)
    image = models.ImageField(upload_to='resort_images/', blank=True, null=True)
    
    location = models.CharField(max_length=255)
    address = models.TextField(blank=True, null=True)   # optional
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)


    def __str__(self):
        return self.name

STATUS_CHOICES = [
    ('Pending', 'Pending'),
    ('Paid', 'Paid'),
    ('Cancelled', 'Cancelled'),
]

class Booking(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True
    )
    resort = models.ForeignKey(Resort, on_delete=models.CASCADE)
    guest_name = models.CharField(max_length=100)
    guest_email = models.EmailField()
    guest_phone = models.CharField(max_length=15)
    check_in = models.DateField()
    check_out = models.DateField()
    guests = models.PositiveIntegerField()
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    booking_status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    payment_status = models.CharField(max_length=10, default='Pending')
    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True)
    
    # NEW FIELDS
    advance_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pending_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    qr_code = models.ImageField(upload_to="qr_codes/", blank=True, null=True)
    checkin_verified = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)


    def __str__(self):
        return f"{self.guest_name} - {self.resort.name}"

class Payment(models.Model):
    booking = models.OneToOneField(Booking, on_delete=models.CASCADE)
    payment_id = models.CharField(max_length=100)
    payment_method = models.CharField(max_length=50)
    payment_time = models.DateTimeField(auto_now_add=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    refunded = models.BooleanField(default=False) 
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    refund_date = models.DateTimeField(blank=True, null=True)
    


    def __str__(self):
        return f"Payment for Booking ID {self.booking.id}"

class Guest(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='guest_list')
    full_name = models.CharField(max_length=100)
    age = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.full_name} (Booking ID: {self.booking.id})"


class Blog(models.Model):
    title = models.CharField(max_length=200)
    excerpt = models.TextField(blank=True, null=True)
    content = models.TextField()
    image = models.ImageField(upload_to='blogs/',null=True)
    date_posted = models.DateField(auto_now_add=True)

    def __str__(self):
        return self.title


class GalleryImage(models.Model):
    title = models.CharField(max_length=100)
    image = models.ImageField(upload_to='gallery/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title
# models.py
class Offer(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()
    discount_percent = models.IntegerField()
    valid_until = models.DateTimeField()
    image = models.ImageField(upload_to='offers/', blank=True, null=True)

class Wishlist(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    resort = models.ForeignKey(Resort, on_delete=models.CASCADE)
    added_on = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} -> {self.resort.name}"
    



class CustomUserManager(BaseUserManager):
    def create_user(self, email, phone, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")

        email = self.normalize_email(email)

        user = self.model(
            email=email,
            phone=phone,
            **extra_fields
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, phone, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        return self.create_user(email, phone, password, **extra_fields)


class CustomUser(AbstractUser):
    username = None # Removed username
    name = models.CharField(max_length=150, default="User")

    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=15, unique=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['phone']

    objects = CustomUserManager()  # <-- IMPORTANT

    def __str__(self):
        return self.email


class PasswordResetOTP(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def __str__(self):
        return f"OTP for {self.user.username} - {self.otp}"