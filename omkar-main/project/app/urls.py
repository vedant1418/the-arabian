from django.urls import path
from . import views
from django.contrib.auth.views import LogoutView

urlpatterns = [

    # ---------------- API ----------------
    path('api/resorts/', views.resort_list, name='resort_list'),
    path('api/book/', views.create_booking, name='create_booking'),
    path('api/payment/', views.confirm_payment, name='confirm_payment'),
    # path('api/booking/<int:booking_id>/', views.booking_detail, name='booking_detail'),

    # ---------------- Web Pages ----------------
    path('', views.index, name='index'),
    path('resort/<int:resort_id>/', views.resort_detail, name='resort_detail'),
    path('resort/<int:resort_id>/book/', views.book_resort, name='book_resort'),

    path('about/', views.about_us, name='about_us'),
    path('blog/', views.blog, name='blog'),
    path('blog/<int:blog_id>/', views.blog_detail, name='blog_detail'),
    path('contact/', views.contact, name='contact'),
    path('gallery/', views.gallery_view, name='gallery'),
    path('events/', views.events, name='events'),
    path('testimonials/', views.testimonials, name='testimonials'),
    path('faq/', views.faq, name='faq'),
    path('team/', views.team, name='team'),

    # ---------------- Refund / Receipt ----------------
    path('refund/<str:payment_id>/', views.refund_payment, name='refund_payment'),
    path('booking/confirmation/<int:booking_id>/', views.booking_confirmation, name='booking_confirmation'),
    path('booking/<int:booking_id>/download-receipt/', views.download_receipt, name='download_receipt'),

    # ---------------- Authentication ----------------
    path('register/', views.register, name='register'),
    path('signin/', views.signin, name='signin'),
    path('logout/', views.userlogout, name='logout'),   # ONLY ONE LOGOUT

    # Password reset correct
    path("request-password-reset/", views.request_password_reset, name="request_password_reset"),

    path('password-reset/verify-otp/', views.verify_reset_otp, name='verify_reset_otp'),
    path('password-reset/new-password/', views.reset_password, name='reset_password'),

    # ---------------- Profile ----------------
    path('profile/', views.profile, name='profile'),

    # ---------------- Wishlist ----------------
    path("wishlist/", views.wishlist_page, name="wishlist"),
    path("wishlist/remove/<int:resort_id>/", views.remove_from_wishlist, name="remove_from_wishlist"),
    path("add-to-wishlist/<int:resort_id>/", views.add_to_wishlist, name="add_to_wishlist"),
    path("wishlist-toggle/<int:resort_id>/", views.wishlist_toggle, name="wishlist_toggle"),
    path("ajax/wishlist/add/<int:resort_id>/", views.ajax_add_wishlist, name="ajax_add_wishlist"),

    # ---------------- Booking History & Refund Actions ----------------
    path("booking-history/", views.booking_history, name="booking_history"),
    path("booking/cancel/<int:booking_id>/", views.cancel_booking, name="cancel_booking"),
    path("booking/refund/<int:booking_id>/", views.refund_booking, name="refund_booking"),
    path("booking/<int:booking_id>/", views.booking_detail, name="booking_detail"),
    path("payment/<int:booking_id>/", views.payment_page, name="payment_page"),

    
    path("verify-checkin/<int:booking_id>/", views.verify_checkin, name="verify_checkin"),
    path("admin/dashboard/", views.admin_dashboard, name="admin_dashboard"),



]
