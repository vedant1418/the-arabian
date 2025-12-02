from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from django.http import HttpResponse
from django.conf import settings
import os
from django.http import JsonResponse
import json
import logging
import base64
import qrcode
from io import BytesIO
from datetime import datetime
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
import razorpay
from xhtml2pdf import pisa

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail, EmailMessage
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import get_template
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import get_user_model

from .models import Resort, Booking, Payment, Blog, GalleryImage, Wishlist
from .forms import GalleryImageForm
from django.utils import timezone


logger = logging.getLogger(__name__)
User = get_user_model()

# -------------------------------------------------------------------
#                         API VIEWS
# -------------------------------------------------------------------

def resort_list(request):
    resorts = Resort.objects.all()
    data = [
        {
            'id': r.id,
            'name': r.name,
            'location': r.location,
            'description': r.description,
            'price_per_guest': float(r.price_per_guest),
            'image': r.image.url if r.image else None,
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
        resort = get_object_or_404(Resort, id=data['resort_id'])

        check_in = parse_date(data['check_in'])
        check_out = parse_date(data['check_out'])
        guests = int(data['guests'])
        days = (check_out - check_in).days or 1

        amount = guests * resort.price_per_guest * days

        booking = Booking.objects.create(
            guest_name=data['guest_name'],
            guest_email=data['guest_email'],
            guest_phone=data['phone'],
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
            amount_paid=booking.total_price
        )

        booking.payment_status = "Paid"
        booking.save()

        send_receipt_email(booking)

        return JsonResponse({
            "status": "Payment confirmed",
            "redirect_url": f"/booking/confirmation/{booking.id}/"
        })

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


# -------------------------------------------------------------------
#                         BOOKING FLOW
# -------------------------------------------------------------------

from datetime import datetime
from django.conf import settings
from django.shortcuts import render, get_object_or_404, redirect
import razorpay
from .models import Resort, Booking

@login_required(login_url='/signin/')
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

        # --------------------------------------------------------------------
        # 🚫 Prevent double booking (User rapidly clicks button or refreshes)
        # --------------------------------------------------------------------
        last_booking = Booking.objects.filter(
            user=request.user,
            resort=resort,
            check_in=check_in_d,
            check_out=check_out_d,
        ).order_by("-id").first()

        if last_booking:
            # If same booking created within last 10 seconds → reuse it
            if (timezone.now() - last_booking.created_at).seconds < 10:
                return redirect("payment_page", booking_id=last_booking.id)

        # --------------------------------------------------------------------
        # ✔ Create booking ONLY once
        # --------------------------------------------------------------------
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
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        order = client.order.create({
            "amount": int(advance * 100),
            "currency": "INR",
            "payment_capture": 1,
        })

        booking.razorpay_order_id = order["id"]
        booking.save()

        # --------------------------------------------------------------------
        # ✔ Redirect to avoid duplicate POST
        # --------------------------------------------------------------------
        return redirect("payment_page", booking_id=booking.id)

    return render(request, "booking_form.html", {"resort": resort})

from django.conf import settings
import razorpay
from datetime import datetime

def payment_page(request, booking_id):
    booking = Booking.objects.get(id=booking_id)

    # Days Calculation (if needed)
    days = (booking.check_out - booking.check_in).days
    if days <= 0:
        days = 1

    # Advance: ₹50 × guests
    amount = booking.guests * 50        # amount in rupees
    pay_amount_paise = amount * 100     # convert to paise for Razorpay

    # Razorpay Client
    client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

    # Create Razorpay Order
    order = client.order.create({
        "amount": pay_amount_paise,
        "currency": "INR",
        "receipt": f"receipt_{booking.id}",
        "payment_capture": 1
    })

    # Save Order ID in DB
    booking.razorpay_order_id = order["id"]
    booking.save()

    return render(request, "payment_page.html", {
        "booking": booking,
        "days": days,
        "amount": amount,                 # ₹ amount
        "order_id": order["id"],          # Razorpay order ID
        "razorpay_key": settings.RAZORPAY_KEY_ID,
    })


@login_required
def booking_history(request):
    # We link bookings by email = logged in user's email
    print("BOOKINGS:", Booking.objects.all())
    for b in Booking.objects.all():
        print("ID:", b.id, "USER:", b.user, "EMAIL:", b.guest_email)

    bookings = Booking.objects.filter(user=request.user).order_by('-id')
    return render(request, "booking_history.html", {"bookings": bookings})

@login_required
def cancel_booking(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id, guest_email=request.user.email)

    if booking.booking_status == "Cancelled":
        messages.warning(request, "This booking is already cancelled.")
        return redirect("booking_history")

    if booking.payment_status == "Paid":
        messages.error(request, "Paid bookings cannot be cancelled directly. Please request a refund.")
        return redirect("booking_history")

    booking.booking_status = "Cancelled"
    booking.save()

    messages.success(request, "Booking cancelled successfully.")
    return redirect("booking_history")
def booking_detail(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    # Generate QR Code again (optional)
    import qrcode
    from io import BytesIO
    import base64

    qr = qrcode.make(f"Booking ID: {booking.id}, Name: {booking.guest_name}")
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    qr_code = base64.b64encode(buffer.getvalue()).decode()

    context = {
        "booking": booking,
        "qr_code": qr_code
    }
    return render(request, "booking_detail.html", context)


# -------------------------------------------------------------------
#                         BASIC PAGES
# -------------------------------------------------------------------

def index(request):
    resorts = Resort.objects.all()

    if request.GET.get("location"):
        resorts = resorts.filter(location__icontains=request.GET["location"])
    if request.GET.get("search"):
        resorts = resorts.filter(name__icontains=request.GET["search"])

    return render(request, "index.html", {
        "resorts": resorts,
        "locations": Resort.objects.values_list("location", flat=True).distinct(),
    })


def resort_detail(request, resort_id):
    return render(request, "resort_detail.html", {"resort": get_object_or_404(Resort, id=resort_id)})


def about_us(request): return render(request, "about_us.html")
def events(request): return render(request, "events.html")
def testimonials(request): return render(request, "testimonials.html")
def faq(request): return render(request, "faq.html")
def team(request): return render(request, "team.html")
def contact(request): return render(request, "contact.html")


# -------------------------------------------------------------------
#                         BLOG
# -------------------------------------------------------------------
def blog(request):
    """Alias for blog list page."""
    return blog_list(request)


def blog_list(request):
    return render(request, "blog.html", {"blogs": Blog.objects.all().order_by("-date_posted")})


def blog_detail(request, blog_id):
    return render(request, "blog_detail.html", {"blog": get_object_or_404(Blog, id=blog_id)})


# -------------------------------------------------------------------
#                         GALLERY
# -------------------------------------------------------------------
def gallery_view(request):
    return gallery(request)


def gallery(request):
    return render(request, "gallery.html", {"images": GalleryImage.objects.all().order_by("-uploaded_at")})


def upload_image(request):
    form = GalleryImageForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("gallery")
    return render(request, "upload_image.html", {"form": form})


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
        [booking.guest_email]
    )

    return redirect("booking_confirmation", booking.id)
@login_required
def refund_booking(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    # SECURITY CHECK
    if booking.guest_email != request.user.email:
        messages.error(request, "This booking does not belong to your account.")
        return redirect("booking_history")

    # Try to find payment entry
    payment = Payment.objects.filter(booking=booking).first()

    # CASE 1 — No payment record (partial advance booking)
    if not payment:
        # Create a dummy payment object so refund works smoothly
        payment = Payment.objects.create(
            booking=booking,
            payment_id=booking.razorpay_order_id or "ADVANCE_ONLY",
            payment_method="Online / Partial",
            amount_paid=booking.advance_paid,
            refunded=False
        )

    # CASE 2 — Already refunded
    if payment.refunded:
        messages.warning(request, "Refund already processed.")
        return redirect("booking_history")

    # CASE 3 — Process refund (manual because it's only advance amount)
    payment.refunded = True
    payment.refund_amount = payment.amount_paid
    payment.refund_date = timezone.now()
    payment.save()

    booking.payment_status = "Refunded"
    booking.booking_status = "Cancelled"
    booking.save()

    messages.success(request, "Your refund request has been processed successfully! Refund of ₹{} has been issued.".format(payment.amount_paid))
    return redirect("booking_history")



def booking_confirmation(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    qr = qrcode.make(request.build_absolute_uri())
    buffer = BytesIO()
    qr.save(buffer)
    img = base64.b64encode(buffer.getvalue()).decode()

    return render(request, "booking_confirmed.html", {"booking": booking, "qr_code": img})

def booking_detail(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)
    return render(request, "booking_detail.html", {"booking": booking})





def send_receipt_email(booking):
    html = get_template("receipt.html").render({"booking": booking})
    pdf = BytesIO()
    pisa.CreatePDF(BytesIO(html.encode("utf-8")), dest=pdf)

    mail = EmailMessage(
        "Your Receipt",
        "Attached is your receipt.",
        settings.EMAIL_HOST_USER,
        [booking.guest_email],
    )
    mail.attach("receipt.pdf", pdf.getvalue(), "application/pdf")
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

def register(request):
    if request.user.is_authenticated:
        return redirect("index")

    if request.method == "POST":
        uname = request.POST.get("uname", "").strip()
        uemail = request.POST.get("uemail", "").strip().lower()
        uphone = request.POST.get("uphone", "").strip()
        upass = request.POST.get("upass", "")
        ucpass = request.POST.get("ucpass", "")

        # Required fields
        if not all([uname, uemail, uphone, upass, ucpass]):
            messages.error(request, "All fields are required.")
            return redirect("register")

        # Password match
        if upass != ucpass:
            messages.error(request, "Passwords do not match.")
            return redirect("register")

        # Username unique
        if User.objects.filter(username__iexact=uname).exists():
            messages.error(request, "Username already exists.")
            return redirect("register")

        # Clean email duplicates
        if User.objects.filter(email__iexact=uemail).exists():
            messages.error(request, "Email already exists.")
            return redirect("register")

        # Phone number duplicate
        if User.objects.filter(phone=uphone).exists():
            messages.error(request, "Phone number already registered.")
            return redirect("register")

        # Strong password check
        try:
            validate_password(upass)
        except Exception as e:
            messages.error(request, str(e))
            return redirect("register")

        # Create user
        user = User.objects.create_user(
            username=uname,
            email=uemail,
            phone=uphone,
            password=upass
        )

        messages.success(request, "Account created successfully! Please login.")
        return redirect("signin")

    return render(request, "register.html")


# -------------------------------------------------------------------
#                         LOGIN (username/email/phone)
# -------------------------------------------------------------------

def signin(request):
    if request.method == "POST":
        identifier = request.POST["uname"]
        password = request.POST["upass"]

        user = None

        if User.objects.filter(username=identifier).exists():
            user = authenticate(username=identifier, password=password)

        elif User.objects.filter(email=identifier).exists():
            u = User.objects.get(email=identifier)
            user = authenticate(username=u.username, password=password)

        elif User.objects.filter(phone=identifier).exists():
            u = User.objects.get(phone=identifier)
            user = authenticate(username=u.username, password=password)

        if user:
            login(request, user)
            return redirect("index")

        return render(request, "signin.html", {"errmsg": "Invalid credentials."})

    return render(request, "signin.html")


def userlogout(request):
    logout(request)
    return redirect("index")


# -------------------------------------------------------------------
#                         PASSWORD RESET
# -------------------------------------------------------------------

import random
from .models import PasswordResetOTP

def request_password_reset(request):
    context = {}
    if request.method == "POST":
        identifier = request.POST.get("identifier")  # username or email

        User = get_user_model()
        try:
            if "@" in identifier:
                user = User.objects.get(email=identifier)
            else:
                user = User.objects.get(username=identifier)
        except User.DoesNotExist:
            context["errmsg"] = "No account found with that username or email."
            return render(request, "request_password_reset.html", context)

        # generate 6-digit OTP
        code = f"{random.randint(100000, 999999)}"
        PasswordResetOTP.objects.create(user=user, otp=code)


        # send email
        send_mail(
            subject="Your Password Reset OTP - The Arabian",
            message=f"Your OTP for password reset is: {code}",
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[user.email],
            fail_silently=False,
        )

        # store user id in session
        request.session["reset_user_id"] = user.id

        return redirect("verify_reset_otp")

    return render(request, "request_password_reset.html", context)

def verify_reset_otp(request):
    context = {}
    user_id = request.session.get("reset_user_id")

    if not user_id:
        return redirect("request_password_reset")

    User = get_user_model()
    user = User.objects.get(id=user_id)

    if request.method == "POST":
        code = request.POST.get("otp")

        otp_obj = PasswordResetOTP.objects.filter(
            user=user, otp=code, is_used=False
        ).order_by("-created_at").first()

        if not otp_obj:
            context["errmsg"] = "Invalid or expired OTP."
        else:
            otp_obj.is_used = True
            otp_obj.save()
            request.session["otp_verified"] = True
            return redirect("reset_password")

    return render(request, "verify_reset_otp.html", context)


def reset_password(request):
    user_id = request.session.get("reset_user_id")
    otp_ok = request.session.get("otp_verified")

    if not (user_id and otp_ok):
        return redirect("request_password_reset")

    User = get_user_model()
    user = User.objects.get(id=user_id)
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
                # clean session
                request.session.pop("reset_user_id", None)
                request.session.pop("otp_verified", None)
                messages.success(request, "Password reset successful! Please login.")
                return redirect("signin")
            except ValidationError as e:
                context["errmsg"] = str(e)

    return render(request, "reset_password.html", context)



# -------------------------------------------------------------------
#                         PROFILE
# -------------------------------------------------------------------

def profile(request):
    return render(request, "profile.html", {"user": request.user})


# -------------------------------------------------------------------
#                         WISHLIST
# -------------------------------------------------------------------
def wishlist_page(request):
    if not request.user.is_authenticated:
        return redirect("signin")

    items = Wishlist.objects.filter(user=request.user)
    return render(request, "wishlist.html", {"items": items})


def add_to_wishlist(request, resort_id):
    if not request.user.is_authenticated:
        return redirect("signin")
    

    resort = get_object_or_404(Resort, id=resort_id)
    Wishlist.objects.get_or_create(user=request.user, resort=resort)

    messages.success(request, "Added to wishlist!")
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

    return JsonResponse({
        "in_wishlist": in_wishlist,
        "message": message,
        "count": count,
    })
@require_POST
def ajax_add_wishlist(request, resort_id):
    if not request.user.is_authenticated:
        return JsonResponse({"status": "error", "message": "Please login first!"})

    resort = get_object_or_404(Resort, id=resort_id)

    wishlist, created = Wishlist.objects.get_or_create(
        user=request.user,
        resort=resort
    )

    if created:
        return JsonResponse({"status": "added", "message": "Added to wishlist!"})
    else:
        wishlist.delete()
        return JsonResponse({"status": "removed", "message": "Removed from wishlist!"})

def verify_checkin(request, booking_id):
    booking = get_object_or_404(Booking, id=booking_id)

    if booking.checkin_verified:
        status = "Already Checked-In"
    else:
        booking.checkin_verified = True
        booking.save()
        status = "Check-In Successful"

    return render(request, "verify_checkin.html", {
        "booking": booking,
        "status": status
    })

def download_receipt(request, booking_id):
    booking = Booking.objects.get(id=booking_id)

    # Create response as PDF
    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f"attachment; filename=Receipt_{booking.id}.pdf"

    p = canvas.Canvas(response, pagesize=A4)
    width, height = A4

    y = height - 60

    # Resort Logo (Optional)
    logo_path = os.path.join(settings.BASE_DIR, "static", "images", "logo.png")
    if os.path.exists(logo_path):
        p.drawImage(logo_path, 40, y - 80, width=120, preserveAspectRatio=True)
    p.setFont("Helvetica-Bold", 20)
    p.drawString(180, y - 40, "Manthan Resorts")
    p.setFont("Helvetica", 11)
    p.drawString(180, y - 60, "Premium Resort Booking Receipt")

    y -= 120

    # Header Line
    p.line(40, y, width - 40, y)
    y -= 30

    p.setFont("Helvetica-Bold", 16)
    p.drawString(40, y, f"Booking Receipt")
    y -= 30

    p.setFont("Helvetica-Bold", 13)
    p.drawString(40, y, f"Booking ID: #{booking.id}")
    y -= 25

    # Guest Details
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

    # Stay Details
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

    # Payment Summary
    p.setFont("Helvetica-Bold", 14)
    p.drawString(40, y, "Payment Summary")
    y -= 20
    p.setFont("Helvetica", 12)
    p.drawString(40, y, f"Advance Paid: ₹{booking.advance_paid}")
    y -= 18
    p.drawString(40, y, f"Pending at Check-in: ₹{booking.pending_amount}")
    y -= 25

    y_space_for_qr = y

    # QR Code on the right side
    if booking.qr_code:
        qr_path = booking.qr_code.path
        if os.path.exists(qr_path):
            p.drawImage(qr_path, width - 200, y_space_for_qr - 140, width=130, height=130)

    y -= 160

    # Footer
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
from django.db.models import Sum, Count
from django.db.models.functions import TruncMonth
from datetime import date
@login_required
def admin_dashboard(request):
    # Only allow staff/admin
    if not request.user.is_staff:
        return redirect("home")

    # Top cards
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

    # Monthly revenue data
    monthly_data = (
        Booking.objects.filter(payment_status="Paid")
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(
            revenue=Sum("total_price"),
            bookings=Count("id")
        )
        .order_by("month")
    )

    # Convert to simple lists for Chart.js
    labels = [item["month"].strftime("%b %Y") for item in monthly_data]
    revenue_data = [float(item["revenue"]) for item in monthly_data]
    bookings_data = [item["bookings"] for item in monthly_data]

    # Top 5 resorts by bookings
    top_resorts_qs = (
        Booking.objects.values("resort__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )

    top_resort_labels = [item["resort__name"] for item in top_resorts_qs]
    top_resort_counts = [item["count"] for item in top_resorts_qs]

    # ⭐ FIX: create zipped list for easy looping in template
    top_resorts = list(zip(top_resort_labels, top_resort_counts))

    context = {
        "total_users": total_users,
        "total_bookings": total_bookings,
        "total_revenue": total_revenue,
        "today_bookings": today_bookings,

        "labels": labels,
        "revenue_data": revenue_data,
        "bookings_data": bookings_data,

        # Pass zipped list
        "top_resorts": top_resorts,

        # Also pass originals for charts
        "top_resort_labels": top_resort_labels,
        "top_resort_counts": top_resort_counts,
    }

    return render(request, "admin_dashboard.html", context)
