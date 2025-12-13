import os
import json
import base64
import logging
import random
from io import BytesIO
from datetime import datetime, date

import qrcode
import razorpay

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.mail import send_mail, EmailMessage
from django.db.models import Sum, Count
from django.db.models.functions import TruncMonth
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import get_template
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth import password_validation
# OR:
# from django.contrib.auth.password_validation import validate_password



# from xhtml2pdf import pisa

from .models import Resort, Booking, Payment, Blog, GalleryImage, Wishlist, PasswordResetOTP
from .forms import GalleryImageForm

logger = logging.getLogger(__name__)
User = get_user_model()

# -------------------------------------------------------------------
#                         HELPERS
# -------------------------------------------------------------------


def get_wishlist_count(request):
    if request.user.is_authenticated:
        return Wishlist.objects.filter(user=request.user).count()
    return 0


# -------------------------------------------------------------------
#                         API VIEWS
# -------------------------------------------------------------------


def resort_list(request):
    resorts = Resort.objects.all()
    data = [
        {
            "id": r.id,
            "name": r.name,
            "location": r.location,
            "description": r.description,
            "price_per_guest": float(r.price_per_guest),
            "image": r.image.url if r.image else None,
        }
        for r in resorts
    ]
    return JsonResponse(data, safe=False)


@csrf_exempt
def create_booking(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=405)

    try:
        data = json.loads(request.body)
        resort = get_object_or_404(Resort, id=data["resort_id"])

        check_in = parse_date(data["check_in"])
        check_out = parse_date(data["check_out"])
        guests = int(data["guests"])
        days = (check_out - check_in).days or 1

        amount = guests * resort.price_per_guest * days

        booking = Booking.objects.create(
            guest_name=data["guest_name"],
            guest_email=data["guest_email"],
            guest_phone=data["phone"],
            resort=resort,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            total_price=amount,
        )

        return JsonResponse({"booking_id": booking.id, "amount": float(amount)})

    except Exception as e:
        logger.error(e)
        return JsonResponse({"error": "Invalid data"}, status=400)


@csrf_exempt
def confirm_payment(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=405)

    try:
        data = json.loads(request.body)
        booking = get_object_or_404(Booking, id=data["booking_id"])

        Payment.objects.create(
            booking=booking,
            payment_id=data["payment_id"],
            payment_method=data.get("payment_method", "Online"),
            amount_paid=booking.total_price,
        )

        booking.payment_status = "Paid"
        booking.save()

        send_receipt_email(booking)

        return JsonResponse(
            {
                "status": "Payment confirmed",
                "redirect_url": f"/booking/confirmation/{booking.id}/",
            }
        )

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


# -------------------------------------------------------------------
#                         BOOKING FLOW
# -------------------------------------------------------------------


@login_required(login_url="/signin/")
def book_resort(request, resort_id):
    resort = get_object_or_404(Resort, id=resort_id)

    if request.method == "POST":
        # Extract form data
        guest_name = request.POST["guest_name"]
        guest_email = request.user.email
        guest_phone = request.POST["guest_phone"]

        check_in = request.POST["check_in"]
        check_out = request.POST["check_out"]
        guests = int(request.POST.get("guests", 1))

        check_in_d = datetime.strptime(check_in, "%Y-%m-%d").date()
        check_out_d = datetime.strptime(check_out, "%Y-%m-%d").date()
        days = max((check_out_d - check_in_d).days, 1)

        total_price = resort.price_per_guest * guests * days
        advance_per_guest = getattr(settings, "ADVANCE_PAYMENT_AMOUNT", 50)
        advance = guests * advance_per_guest
        pending = total_price - advance

        # Prevent double booking (User rapidly clicks button or refreshes)
        last_booking = (
            Booking.objects.filter(
                user=request.user,
                resort=resort,
                check_in=check_in_d,
                check_out=check_out_d,
            )
            .order_by("-id")
            .first()
        )

        if last_booking:
            if (timezone.now() - last_booking.created_at).seconds < 10:
                return redirect("payment_page", booking_id=last_booking.id)

        # Create booking
        booking = Booking.objects.create(
            user=request.user,
            resort=resort,
            guest_name=guest_name,
            guest_email=guest_email,
            guest_phone=guest_phone,
            check_in=check_in_d,
            check_out=check_out_d,
            guests=guests,
            total_price=total_price,
            advance_paid=advance,
            pending_amount=pending,
            payment_status="Pending",
        )

        # Generate QR
        qr_url = request.build_absolute_uri(f"/verify-checkin/{booking.id}/")
        qr_img = qrcode.make(qr_url)
        buffer = BytesIO()
        qr_img.save(buffer, format="PNG")
        booking.qr_code.save(f"qr_{booking.id}.png", ContentFile(buffer.getvalue()))

        # Razorpay order creation
        client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
        order = client.order.create(
            {
                "amount": int(advance * 100),
                "currency": "INR",
                "payment_capture": 1,
            }
        )

        booking.razorpay_order_id = order["id"]
        booking.save()

        return redirect("payment_page", booking_id=booking.id)

    return render(
        request,
        "booking_form.html",
        {"resort": resort, "wishlist_count": get_wishlist_count(request)},
    )


def payment_page(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    # Days Calculation
    days = (booking.check_out - booking.check_in).days
    if days <= 0:
        days = 1

    # Advance: ₹50 × guests
    amount = booking.guests * 50  # rupees
    pay_amount_paise = amount * 100  # paise

    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )

    order = client.order.create(
        {
            "amount": pay_amount_paise,
            "currency": "INR",
            "receipt": f"receipt_{booking.id}",
            "payment_capture": 1,
        }
    )

    booking.razorpay_order_id = order["id"]
    booking.save()

    return render(
        request,
        "payment_page.html",
        {
            "booking": booking,
            "days": days,
            "amount": amount,
            "order_id": order["id"],
            "razorpay_key": settings.RAZORPAY_KEY_ID,
            "wishlist_count": get_wishlist_count(request),
        },
    )


@login_required(login_url="/signin/")
def booking_history(request):
    bookings = Booking.objects.filter(user=request.user).order_by("-id")
    return render(
        request,
        "booking_history.html",
        {"bookings": bookings, "wishlist_count": get_wishlist_count(request)},
    )


@login_required(login_url="/signin/")
def cancel_booking(request, booking_id):
    booking = get_object_or_404(
        Booking, id=booking_id, guest_email=request.user.email
    )

    if booking.booking_status == "Cancelled":
        messages.warning(request, "This booking is already cancelled.")
        return redirect("booking_history")

    if booking.payment_status == "Paid":
        messages.error(
            request,
            "Paid bookings cannot be cancelled directly. Please request a refund.",
        )
        return redirect("booking_history")

    booking.booking_status = "Cancelled"
    booking.save()

    messages.success(request, "Booking cancelled successfully.")
    return redirect("booking_history")


def booking_detail(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    qr = qrcode.make(f"Booking ID: {booking.id}, Name: {booking.guest_name}")
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    qr_code = base64.b64encode(buffer.getvalue()).decode()

    context = {
        "booking": booking,
        "qr_code": qr_code,
        "wishlist_count": get_wishlist_count(request),
    }
    return render(request, "booking_detail.html", context)


def booking_confirmation(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    qr = qrcode.make(request.build_absolute_uri())
    buffer = BytesIO()
    qr.save(buffer)
    img = base64.b64encode(buffer.getvalue()).decode()

    return render(
        request,
        "booking_confirmed.html",
        {"booking": booking, "qr_code": img, "wishlist_count": get_wishlist_count(request)},
    )


def verify_checkin(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    if booking.checkin_verified:
        status = "Already Checked-In"
    else:
        booking.checkin_verified = True
        booking.save()
        status = "Check-In Successful"

    return render(
        request,
        "verify_checkin.html",
        {"booking": booking, "status": status, "wishlist_count": get_wishlist_count(request)},
    )


def download_receipt(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f"attachment; filename=Receipt_{booking.id}.pdf"

    p = canvas.Canvas(response, pagesize=A4)
    width, height = A4

    y = height - 60

    # Logo (optional)
    logo_path = os.path.join(settings.BASE_DIR, "static", "images", "logo.png")
    if os.path.exists(logo_path):
        p.drawImage(logo_path, 40, y - 80, width=120, preserveAspectRatio=True)
    p.setFont("Helvetica-Bold", 20)
    p.drawString(180, y - 40, "Manthan Resorts")
    p.setFont("Helvetica", 11)
    p.drawString(180, y - 60, "Premium Resort Booking Receipt")

    y -= 120
    p.line(40, y, width - 40, y)
    y -= 30

    p.setFont("Helvetica-Bold", 16)
    p.drawString(40, y, "Booking Receipt")
    y -= 30

    p.setFont("Helvetica-Bold", 13)
    p.drawString(40, y, f"Booking ID: #{booking.id}")
    y -= 25

    # Guest details
    p.setFont("Helvetica-Bold", 14)
    p.drawString(40, y, "Guest Information")
    y -= 20
    p.setFont("Helvetica", 12)
    p.drawString(40, y, f"Name: {booking.guest_name}")
    y -= 18
    p.drawString(40, y, f"Email: {booking.guest_email}")
    y -= 18
    p.drawString(40, y, f"Phone: {booking.guest_phone}")
    y -= 32

    # Stay details
    p.setFont("Helvetica-Bold", 14)
    p.drawString(40, y, "Stay Details")
    y -= 20
    p.setFont("Helvetica", 12)
    p.drawString(40, y, f"Resort: {booking.resort.name}")
    y -= 18
    p.drawString(40, y, f"Check-in: {booking.check_in}")
    y -= 18
    p.drawString(40, y, f"Check-out: {booking.check_out}")
    y -= 18
    p.drawString(40, y, f"Guests: {booking.guests}")
    y -= 32

    # Payment summary
    p.setFont("Helvetica-Bold", 14)
    p.drawString(40, y, "Payment Summary")
    y -= 20
    p.setFont("Helvetica", 12)
    p.drawString(40, y, f"Advance Paid: ₹{booking.advance_paid}")
    y -= 18
    p.drawString(40, y, f"Pending at Check-in: ₹{booking.pending_amount}")
    y -= 25

    y_space_for_qr = y

    if booking.qr_code and os.path.exists(booking.qr_code.path):
        p.drawImage(
            booking.qr_code.path, width - 200, y_space_for_qr - 140, width=130, height=130
        )

    y -= 160

    p.setFont("Helvetica-Bold", 12)
    p.drawString(40, y, "Thank you for choosing Manthan Resorts!")
    y -= 15
    p.setFont("Helvetica", 10)
    p.drawString(40, y, "This receipt serves as proof of advance booking payment.")
    y -= 12
    p.drawString(40, y, "Show the QR code at the resort for smooth check-in.")

    p.showPage()
    p.save()

    return response


# -------------------------------------------------------------------
#                         BASIC PAGES
# -------------------------------------------------------------------


def index(request):
    resorts = Resort.objects.all()

    if request.GET.get("location"):
        resorts = resorts.filter(location__icontains=request.GET["location"])
    if request.GET.get("search"):
        resorts = resorts.filter(name__icontains=request.GET["search"])

    return render(
        request,
        "index.html",
        {
            "resorts": resorts,
            "locations": Resort.objects.values_list(
                "location", flat=True
            ).distinct(),
            "wishlist_count": get_wishlist_count(request),
        },
    )

@login_required (login_url="/signin/")
def resort_detail(request, resort_id):
    resort = get_object_or_404(Resort, id=resort_id)
    return render(
        request,
        "resort_detail.html",
        {"resort": resort, "wishlist_count": get_wishlist_count(request)},
    )


def about_us(request):
    return render(request, "about_us.html", {"wishlist_count": get_wishlist_count(request)})


def events(request):
    return render(request, "events.html", {"wishlist_count": get_wishlist_count(request)})


def testimonials(request):
    return render(request, "testimonials.html", {"wishlist_count": get_wishlist_count(request)})


def faq(request):
    return render(request, "faq.html", {"wishlist_count": get_wishlist_count(request)})


def team(request):
    return render(request, "team.html", {"wishlist_count": get_wishlist_count(request)})


def contact(request):
    return render(request, "contact.html", {"wishlist_count": get_wishlist_count(request)})


# -------------------------------------------------------------------
#                         BLOG
# -------------------------------------------------------------------


def blog(request):
    return blog_list(request)


def blog_list(request):
    blogs = Blog.objects.all().order_by("-date_posted")
    return render(
        request, "blog.html", {"blogs": blogs, "wishlist_count": get_wishlist_count(request)}
    )


def blog_detail(request, blog_id):
    blog_obj = get_object_or_404(Blog, id=blog_id)
    return render(
        request,
        "blog_detail.html",
        {"blog": blog_obj, "wishlist_count": get_wishlist_count(request)},
    )


# -------------------------------------------------------------------
#                         GALLERY
# -------------------------------------------------------------------


def gallery_view(request):
    return gallery(request)


def gallery(request):
    images = GalleryImage.objects.all().order_by("-uploaded_at")
    return render(
        request,
        "gallery.html",
        {"images": images, "wishlist_count": get_wishlist_count(request)},
    )


def upload_image(request):
    form = GalleryImageForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("gallery")
    return render(
        request,
        "upload_image.html",
        {"form": form, "wishlist_count": get_wishlist_count(request)},
    )


# -------------------------------------------------------------------
#                         RECEIPTS & REFUNDS
# -------------------------------------------------------------------


@require_POST
def refund_payment(request, payment_id):
    payment = get_object_or_404(Payment, payment_id=payment_id)
    booking = payment.booking
    booking.payment_status = "Refunded"
    booking.save()

    send_mail(
        "Refund Processed",
        f"Your refund of ₹{booking.total_price} is processed.",
        settings.EMAIL_HOST_USER,
        [booking.guest_email],
    )

    return redirect("booking_confirmation", booking.id)


@login_required(login_url="/signin/")
def refund_booking(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    if booking.guest_email != request.user.email:
        messages.error(request, "This booking does not belong to your account.")
        return redirect("booking_history")

    payment = Payment.objects.filter(booking=booking).first()

    if not payment:
        payment = Payment.objects.create(
            booking=booking,
            payment_id=booking.razorpay_order_id or "ADVANCE_ONLY",
            payment_method="Online / Partial",
            amount_paid=booking.advance_paid,
            refunded=False,
        )

    if payment.refunded:
        messages.warning(request, "Refund already processed.")
        return redirect("booking_history")

    payment.refunded = True
    payment.refund_amount = payment.amount_paid
    payment.refund_date = timezone.now()
    payment.save()

    booking.payment_status = "Refunded"
    booking.booking_status = "Cancelled"
    booking.save()

    messages.success(
        request,
        f"Your refund request has been processed successfully! Refund of ₹{payment.amount_paid} has been issued.",
    )
    return redirect("booking_history")


def send_receipt_email(booking):
    html = get_template("receipt.html").render({"booking": booking})
    pdf_buffer = BytesIO()
    pisa.CreatePDF(BytesIO(html.encode("utf-8")), dest=pdf_buffer)

    mail = EmailMessage(
        "Your Receipt",
        "Attached is your receipt.",
        settings.EMAIL_HOST_USER,
        [booking.guest_email],
    )
    mail.attach("receipt.pdf", pdf_buffer.getvalue(), "application/pdf")
    mail.send()


# -------------------------------------------------------------------
#                         PASSWORD RULES
# -------------------------------------------------------------------


def validate_password(password):
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters.")
    if not any(c.isupper() for c in password):
        raise ValidationError("At least one uppercase letter required.")
    if not any(c.islower() for c in password):
        raise ValidationError("At least one lowercase letter required.")
    if not any(c.isdigit() for c in password):
        raise ValidationError("Password must contain a number.")
    if not any(c in "@$!%*?&" for c in password):
        raise ValidationError("Password must contain a special character.")


# -------------------------------------------------------------------
#                         REGISTER
# -------------------------------------------------------------------


User = get_user_model()

def register(request):

    # If logged in → no need to register again
    if request.user.is_authenticated:
        return redirect("index")

    if request.method == "POST":
        uname = request.POST.get("uname", "").strip()
        uemail = request.POST.get("uemail", "").strip().lower()
        uphone = request.POST.get("uphone", "").strip()
        upass = request.POST.get("upass", "")
        ucpass = request.POST.get("ucpass", "")

        # ---- VALIDATIONS ----
        if not all([uname, uemail, uphone, upass, ucpass]):
            messages.error(request, "All fields are required.", extra_tags="register")
            return redirect("register")

        if upass != ucpass:
            messages.error(request, "Passwords do not match.", extra_tags="register")
            return redirect("register")

        if User.objects.filter(email__iexact=uemail).exists():
            messages.error(request, "Email already exists.", extra_tags="register")
            return redirect("register")

        if User.objects.filter(phone=uphone).exists():
            messages.error(request, "Phone number already registered.", extra_tags="register")
            return redirect("register")

        # Django password validation
        try:
            password_validation.validate_password(upass)
        except Exception as e:
            messages.error(request, str(e), extra_tags="register")
            return redirect("register")

        # ---- CREATE USER ----
        user = User.objects.create_user(
            email=uemail,
            phone=uphone,
            password=upass,
        )
        user.name = uname
        user.save()

        messages.success(request, "Account created successfully! Please login.", extra_tags="register")
        return redirect("signin")

    # ---- VERY IMPORTANT: return template for GET ----
    return render(request, "register.html")
    
# -------------------------------------------------------------------
#                         LOGIN (EMAIL ONLY)
# -------------------------------------------------------------------


def signin(request):
    if request.method == "POST":
        # Support either name="uemail" or legacy "uname" field in template
        email = (
            request.POST.get("uemail")
            or request.POST.get("uname")
            or ""
        ).strip().lower()
        password = request.POST.get("upass", "")

        if not email or not password:
            return render(
                request,
                "signin.html",
                {
                    "errmsg": "Email and password are required.",
                    "wishlist_count": get_wishlist_count(request),
                },
            )

        user = authenticate(request, username=email, password=password)

        if user:
            login(request, user)
            return redirect("index")

        return render(
            request,
            "signin.html",
            {
                "errmsg": "Invalid email or password.",
                "wishlist_count": get_wishlist_count(request),
            },
        )

    return render(
        request,
        "signin.html",
        {"wishlist_count": get_wishlist_count(request)},
    )


def userlogout(request):
    logout(request)
    return redirect("index")


# -------------------------------------------------------------------
#                         PASSWORD RESET (EMAIL ONLY)
# -------------------------------------------------------------------


def request_password_reset(request):
    context = {}
    if request.method == "POST":
        email = request.POST.get("identifier", "").strip().lower()

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            context["errmsg"] = "No account found with that email."
            context["wishlist_count"] = get_wishlist_count(request)
            return render(request, "request_password_reset.html", context)

        code = f"{random.randint(100000, 999999)}"
        PasswordResetOTP.objects.create(user=user, otp=code)

        send_mail(
            subject="Your Password Reset OTP - The Arabian",
            message=f"Your OTP for password reset is: {code}",
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[user.email],
            fail_silently=False,
        )

        request.session["reset_user_id"] = user.id
        return redirect("verify_reset_otp")

    context["wishlist_count"] = get_wishlist_count(request)
    return render(request, "request_password_reset.html", context)


def verify_reset_otp(request):
    context = {}
    user_id = request.session.get("reset_user_id")

    if not user_id:
        return redirect("request_password_reset")

    user = get_object_or_404(User, id=user_id)

    if request.method == "POST":
        code = request.POST.get("otp")

        otp_obj = (
            PasswordResetOTP.objects.filter(user=user, otp=code, is_used=False)
            .order_by("-created_at")
            .first()
        )

        if not otp_obj:
            context["errmsg"] = "Invalid or expired OTP."
        else:
            otp_obj.is_used = True
            otp_obj.save()
            request.session["otp_verified"] = True
            return redirect("reset_password")

    context["wishlist_count"] = get_wishlist_count(request)
    return render(request, "verify_reset_otp.html", context)


def reset_password(request):
    user_id = request.session.get("reset_user_id")
    otp_ok = request.session.get("otp_verified")

    if not (user_id and otp_ok):
        return redirect("request_password_reset")

    user = get_object_or_404(User, id=user_id)
    context = {}

    if request.method == "POST":
        upass = request.POST.get("upass")
        ucpass = request.POST.get("ucpass")

        if not upass or not ucpass:
            context["errmsg"] = "All fields are required."
        elif upass != ucpass:
            context["errmsg"] = "Passwords do not match."
        else:
            try:
                validate_password(upass)
                user.set_password(upass)
                user.save()

                request.session.pop("reset_user_id", None)
                request.session.pop("otp_verified", None)
                messages.success(request, "Password reset successful! Please login.")
                return redirect("signin")
            except ValidationError as e:
                context["errmsg"] = str(e)

    context["wishlist_count"] = get_wishlist_count(request)
    return render(request, "reset_password.html", context)


# -------------------------------------------------------------------
#                         PROFILE
# -------------------------------------------------------------------


@login_required(login_url="/signin/")
def profile(request):
    return render(
        request,
        "profile.html",
        {"user": request.user, "wishlist_count": get_wishlist_count(request)},
    )


# -------------------------------------------------------------------
#                         WISHLIST
# -------------------------------------------------------------------


def wishlist_page(request):
    if not request.user.is_authenticated:
        return redirect("signin")

    items = Wishlist.objects.filter(user=request.user)
    return render(
        request,
        "wishlist.html",
        {"items": items, "wishlist_count": get_wishlist_count(request)},
    )


def add_to_wishlist(request, resort_id):
    if not request.user.is_authenticated:
        messages.error(request, "Please login to continue.", extra_tags="wishlist")
        return redirect("signin")

    resort = Resort.objects.get(id=resort_id)
    Wishlist.objects.get_or_create(user=request.user, resort=resort)

    messages.success(request, "Added to wishlist!", extra_tags="wishlist")

    return redirect(request.META.get("HTTP_REFERER", "index"))


def remove_from_wishlist(request, resort_id):
    if not request.user.is_authenticated:
        return redirect("signin")

    Wishlist.objects.filter(user=request.user, resort_id=resort_id).delete()
    messages.success(request, "Removed from wishlist!")
    return redirect(request.META.get("HTTP_REFERER", "wishlist"))


def wishlist_toggle(request, resort_id):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login_required"}, status=403)

    resort = get_object_or_404(Resort, id=resort_id)
    existing = Wishlist.objects.filter(user=request.user, resort=resort)

    if existing.exists():
        existing.delete()
        in_wishlist = False
        message = "Removed from wishlist"
    else:
        Wishlist.objects.create(user=request.user, resort=resort)
        in_wishlist = True
        message = "Added to wishlist!"

    count = Wishlist.objects.filter(user=request.user).count()

    return JsonResponse(
        {
            "in_wishlist": in_wishlist,
            "message": message,
            "count": count,
        }
    )


@require_POST
def ajax_add_wishlist(request, resort_id):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"status": "error", "message": "Please login first!"}
        )

    resort = get_object_or_404(Resort, id=resort_id)

    wishlist, created = Wishlist.objects.get_or_create(
        user=request.user, resort=resort
    )

    if created:
        return JsonResponse({"status": "added", "message": "Added to wishlist!"})
    else:
        wishlist.delete()
        return JsonResponse(
            {"status": "removed", "message": "Removed from wishlist!"}
        )


# -------------------------------------------------------------------
#                         ADMIN DASHBOARD
# -------------------------------------------------------------------


@login_required(login_url="/signin/")
def admin_dashboard(request):
    if not request.user.is_staff:
        return redirect("index")

    total_users = User.objects.count()
    total_bookings = Booking.objects.count()
    total_revenue = (
        Booking.objects.filter(payment_status="Paid")
        .aggregate(total=Sum("total_price"))["total"]
        or 0
    )
    today_bookings = Booking.objects.filter(
        created_at__date=date.today()
    ).count()

    monthly_data = (
        Booking.objects.filter(payment_status="Paid")
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(
            revenue=Sum("total_price"),
            bookings=Count("id"),
        )
        .order_by("month")
    )

    labels = [item["month"].strftime("%b %Y") for item in monthly_data]
    revenue_data = [float(item["revenue"]) for item in monthly_data]
    bookings_data = [item["bookings"] for item in monthly_data]

    top_resorts_qs = (
        Booking.objects.values("resort__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )

    top_resort_labels = [item["resort__name"] for item in top_resorts_qs]
    top_resort_counts = [item["count"] for item in top_resorts_qs]

    top_resorts = list(zip(top_resort_labels, top_resort_counts))

    context = {
        "total_users": total_users,
        "total_bookings": total_bookings,
        "total_revenue": total_revenue,
        "today_bookings": today_bookings,
        "labels": labels,
        "revenue_data": revenue_data,
        "bookings_data": bookings_data,
        "top_resorts": top_resorts,
        "top_resort_labels": top_resort_labels,
        "top_resort_counts": top_resort_counts,
        "wishlist_count": get_wishlist_count(request),
    }

    return render(request, "admin_dashboard.html", context)
