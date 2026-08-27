from django.test import TestCase
from datetime import date, time
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.utils import timezone
from .models import Room, Teacher, CourseGroup, Student, Enrollment, Payment, CourseGroupSchedule, Session, SessionChangeHistory
from .services.scheduling.audit import AuditService
from .utils import calculate_enrollment_expected_fee, get_student_payment_status, detect_all_conflicts

class ConflictDetectionTestCase(TestCase):
    def test_detect_all_conflicts_reports_room_and_teacher_conflicts_separately(self):
        room = Room.objects.create(name="Salle A", capacity=2)
        teacher = Teacher.objects.create(
            name="Teacher One",
            phone="0600000000",
            payment_method="PERCENTAGE",
            payment_percentage=Decimal("50.00"),
        )
        course_one = CourseGroup.objects.create(
            name="Math",
            subject="Math",
            monthly_price=Decimal("100.00"),
            teacher=teacher,
        )
        course_two = CourseGroup.objects.create(
            name="Science",
            subject="Science",
            monthly_price=Decimal("100.00"),
            teacher=teacher,
        )

        CourseGroupSchedule.objects.create(
            course_group=course_one,
            day="MON",
            start_time="14:00:00",
            end_time="16:00:00",
            room=room,
        )
        # bulk_create bypasses model-level full_clean(), which is intentional here:
        # this test needs to create a conflicting schedule in order to verify that
        # detect_all_conflicts() surfaces it correctly.
        CourseGroupSchedule.objects.bulk_create([
            CourseGroupSchedule(
                course_group=course_two,
                day="MON",
                start_time="15:00:00",
                end_time="17:00:00",
                room=room,
            )
        ])

        result = detect_all_conflicts()

        self.assertGreaterEqual(len(result['schedule_conflicts']), 1)
        conflict_types = {c['type'] for c in result['schedule_conflicts']}
        self.assertIn('ROOM', conflict_types)


class PaymentLogicTestCase(TestCase):
    def setUp(self):
        self.room = Room.objects.create(name="Salle 101", capacity=30)
        self.teacher = Teacher.objects.create(
            name="Teacher John", 
            phone="12345678", 
            payment_method="PERCENTAGE", 
            payment_percentage=Decimal('50.00')
        )
        self.course = CourseGroup.objects.create(
            name="Math 1",
            subject="Math",
            monthly_price=Decimal('1000.00'),
            teacher=self.teacher
        )
        self.schedule = CourseGroupSchedule.objects.create(
            course_group=self.course,
            day="MON",
            start_time="14:00:00",
            end_time="16:00:00",
            room=self.room
        )
        self.student = Student.objects.create(
            name="Student Alice",
            parent_contact="87654321"
        )

    def test_student_helper_generates_matricule_with_year_prefix(self):
        matricule = Student.generate_next_matricule()
        self.assertTrue(matricule.startswith("M26-"))

    def test_student_generate_next_matricule_above_1000(self):
        used = set(range(1, 1001))
        candidate = Student._build_candidate_matricule("M26-", used)
        self.assertEqual(candidate, "M26-1001")

    def test_payment_helper_generates_unique_receipt_numbers(self):
        first_receipt = Payment.generate_next_receipt_number(2026)
        Payment.objects.create(
            student=self.student,
            amount=Decimal("100.00"),
            payment_date=date(2026, 7, 1),
            month_covered=date(2026, 7, 1),
            receipt_number=first_receipt,
        )
        second_receipt = Payment.generate_next_receipt_number(2026)

        self.assertTrue(first_receipt.startswith("REC2026"))
        self.assertTrue(second_receipt.startswith("REC2026"))
        self.assertTrue(int(first_receipt[-4:]) < int(second_receipt[-4:]))

    def test_future_enrollment_expected_fee_is_zero(self):
        enrollment = Enrollment.objects.create(
            student=self.student,
            course_group=self.course,
            is_active=True
        )
        Enrollment.objects.filter(pk=enrollment.pk).update(enrolled_date=date(2026, 7, 15))
        enrollment.refresh_from_db()
        
        june_date = date(2026, 6, 1)
        expected_fee = calculate_enrollment_expected_fee(enrollment, june_date)
        self.assertEqual(expected_fee, Decimal('0.00'))

    def test_current_month_prorated_expected_fee(self):
        # Enrollment date mid-month: Oct 19, 2026 (Monday)
        # Mondays in October 2026: Oct 5, 12, 19, 26 (4 total)
        # Remaining from Oct 19: Oct 19, 26 (2 remaining)
        # Expected fee = 2/4 * 1000.00 = 500.00
        enrollment = Enrollment.objects.create(
            student=self.student,
            course_group=self.course,
            is_active=True
        )
        Enrollment.objects.filter(pk=enrollment.pk).update(enrolled_date=date(2026, 10, 19))
        enrollment.refresh_from_db()
        
        october_date = date(2026, 10, 1)
        expected_fee = calculate_enrollment_expected_fee(enrollment, october_date)
        self.assertEqual(expected_fee, Decimal('500.00'))

    def test_get_student_payment_status_historical_month(self):
        enrollment = Enrollment.objects.create(
            student=self.student,
            course_group=self.course,
            is_active=True
        )
        Enrollment.objects.filter(pk=enrollment.pk).update(enrolled_date=date(2026, 5, 1))
        enrollment.refresh_from_db()
        
        may_date = date(2026, 5, 1)
        status = get_student_payment_status(self.student, may_date)
        self.assertEqual(status['required'], Decimal('1000.00'))
        self.assertEqual(status['remaining'], Decimal('1000.00'))
        self.assertEqual(status['status'], 'UNPAID')

    def test_current_month_prorated_expected_fee_rounded_up(self):
        # Course price = 500.00, 4 total mondays in Oct 2026.
        # Oct 12 has 3 remaining. Prorated price = 3 * 125.00 = 375.00
        # Round up to nearest multiple of 10 -> 380.00.
        course = CourseGroup.objects.create(
            name="Math 2",
            subject="Math",
            monthly_price=Decimal('500.00'),
            teacher=self.teacher
        )
        alternate_room = Room.objects.create(name="Salle 102", capacity=20)
        alternate_teacher = Teacher.objects.create(
            name="Teacher Two",
            phone="0600000001",
            payment_method="PERCENTAGE",
            payment_percentage=Decimal("50.00"),
        )
        course.refresh_from_db()
        course.teacher = alternate_teacher
        course.save(update_fields=['teacher'])
        CourseGroupSchedule.objects.create(
            course_group=course,
            day="MON",
            start_time="14:00:00",
            end_time="16:00:00",
            room=alternate_room
        )
        enrollment = Enrollment.objects.create(
            student=self.student,
            course_group=course,
            is_active=True
        )
        Enrollment.objects.filter(pk=enrollment.pk).update(enrolled_date=date(2026, 10, 12))
        enrollment.refresh_from_db()

        october_date = date(2026, 10, 1)
        expected_fee = calculate_enrollment_expected_fee(enrollment, october_date)
        self.assertEqual(expected_fee, Decimal('380.00'))

    def test_setup_levels_management_command(self):
        from django.core.management import call_command
        from .models import Level
        
        # Clear existing levels
        Level.objects.all().delete()
        
        # Call management command
        call_command('setup_levels')
        
        # Check that levels are correctly created
        self.assertEqual(Level.objects.filter(category__code='GARDERIE').count(), 3)
        self.assertEqual(Level.objects.filter(category__code='PRIMAIRE').count(), 6)
        self.assertEqual(Level.objects.filter(category__code='COLLEGE').count(), 3)
        self.assertEqual(Level.objects.filter(category__code='LYCEE').count(), 3)
        
        # Check specific levels
        self.assertTrue(Level.objects.filter(name='Petite Section (PS)', category__code='GARDERIE').exists())
        self.assertTrue(Level.objects.filter(name='1AP', category__code='PRIMAIRE').exists())
        self.assertTrue(Level.objects.filter(name='3ASC', category__code='COLLEGE').exists())
        self.assertTrue(Level.objects.filter(name='Tronc Commun (TC)', category__code='LYCEE').exists())


class SchedulingAuditTestCase(TestCase):
    def test_session_audit_log_survives_session_deletion(self):
        user = get_user_model().objects.create_user(username='audit-user', password='secret123')
        room = Room.objects.create(name='Audit Room', capacity=20)
        teacher = Teacher.objects.create(
            name='Audit Teacher',
            phone='0600000000',
            payment_method='PERCENTAGE',
            payment_percentage=Decimal('50.00'),
        )
        course_group = CourseGroup.objects.create(
            name='Audit Group',
            subject='Math',
            monthly_price=Decimal('100.00'),
            teacher=teacher,
        )
        session = Session.objects.create(
            group=course_group,
            date=date(2026, 7, 16),
            start_time=time(14, 0),
            end_time=time(16, 0),
            room=room,
            status='PLANNED',
        )

        AuditService.log_change(
            session=session,
            user=user,
            action='delete',
            previous_values={'status': 'PLANNED'},
            new_values={},
            change_reason='Test delete audit',
        )

        session.delete()

        log = SessionChangeHistory.objects.get(action='delete')
        self.assertIsNone(log.session)
        self.assertEqual(log.change_reason, 'Test delete audit')

    def test_session_change_history_is_registered_in_admin(self):
        from .admin import admin

        registered_models = [model_admin.model for model_admin in admin.site._registry.values()]
        self.assertIn(SessionChangeHistory, registered_models)


class KioskSearchTestCase(TestCase):
    """Tests for Parent Kiosk search logic and session security."""

    def setUp(self):
        self.teacher = Teacher.objects.create(
            name="Prof Test",
            phone="0600000000",
            payment_method="PERCENTAGE",
            payment_percentage=Decimal("50.00"),
        )
        self.student_a = Student.objects.create(
            name="Alice Benali",
            parent_contact="0612345678",
            parent_name="Fatima Benali",
            is_active=True,
        )
        self.student_b = Student.objects.create(
            name="Bilal Benali",
            parent_contact="0612345678",   # Same parent phone — sibling
            parent_name="Fatima Benali",
            is_active=True,
        )
        self.student_c = Student.objects.create(
            name="Chaimae Karimi",
            parent_contact="0698765432",
            parent_name="Said Karimi",
            is_active=True,
        )

    # ── Home page loads without login ────────────────────────────────
    def test_kiosk_home_accessible_without_login(self):
        response = self.client.get('/public/kiosk/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rechercher votre enfant")

    # ── Matricule search → single student ───────────────────────────
    def test_search_by_matricule_redirects_to_student(self):
        response = self.client.post('/public/kiosk/search/', {
            'search_query': self.student_c.matricule,
        })
        self.assertRedirects(response, '/public/kiosk/student/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['kiosk_student_id'], self.student_c.id)

    def test_search_by_matricule_case_insensitive(self):
        response = self.client.post('/public/kiosk/search/', {
            'search_query': self.student_c.matricule.lower(),
        })
        self.assertRedirects(response, '/public/kiosk/student/', fetch_redirect_response=False)

    # ── Phone search → single match ──────────────────────────────────
    def test_search_by_phone_single_match_redirects_to_student(self):
        response = self.client.post('/public/kiosk/search/', {
            'search_query': self.student_c.parent_contact,
        })
        self.assertRedirects(response, '/public/kiosk/student/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['kiosk_student_id'], self.student_c.id)

    # ── Phone search → multiple siblings ────────────────────────────
    def test_search_by_phone_multiple_matches_redirects_to_select(self):
        response = self.client.post('/public/kiosk/search/', {
            'search_query': self.student_a.parent_contact,
        })
        self.assertRedirects(response, '/public/kiosk/select/', fetch_redirect_response=False)
        matched = self.client.session['kiosk_search_matches']
        self.assertIn(self.student_a.id, matched)
        self.assertIn(self.student_b.id, matched)

    # ── No match → redirect home with error ─────────────────────────
    def test_search_no_match_redirects_home(self):
        response = self.client.post('/public/kiosk/search/', {
            'search_query': '0699999999',
        })
        self.assertRedirects(response, '/public/kiosk/', fetch_redirect_response=False)

    # ── Select page requires session ─────────────────────────────────
    def test_select_without_session_redirects_home(self):
        response = self.client.get('/public/kiosk/select/')
        self.assertRedirects(response, '/public/kiosk/', fetch_redirect_response=False)

    # ── Select student validates session whitelist ────────────────────
    def test_select_student_not_in_session_rejected(self):
        # Seed a session with only student_a and student_b
        session = self.client.session
        session['kiosk_search_matches'] = [self.student_a.id, self.student_b.id]
        session.save()

        # Attempt to select student_c (not in the match list)
        response = self.client.get(f'/public/kiosk/select/{self.student_c.id}/')
        self.assertRedirects(response, '/public/kiosk/', fetch_redirect_response=False)
        self.assertNotIn('kiosk_student_id', self.client.session)

    # ── Valid selection from whitelist ────────────────────────────────
    def test_select_student_in_session_succeeds(self):
        session = self.client.session
        session['kiosk_search_matches'] = [self.student_a.id, self.student_b.id]
        session.save()

        response = self.client.get(f'/public/kiosk/select/{self.student_a.id}/')
        self.assertRedirects(response, '/public/kiosk/student/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['kiosk_student_id'], self.student_a.id)
        self.assertNotIn('kiosk_search_matches', self.client.session)

    # ── Student detail page requires session ──────────────────────────
    def test_student_page_without_session_redirects_home(self):
        response = self.client.get('/public/kiosk/student/')
        self.assertRedirects(response, '/public/kiosk/', fetch_redirect_response=False)

    # ── Student detail page renders correctly ─────────────────────────
    def test_student_page_renders_with_valid_session(self):
        session = self.client.session
        session['kiosk_student_id'] = self.student_c.id
        session.save()

        response = self.client.get('/public/kiosk/student/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Chaimae Karimi")
        self.assertContains(response, self.student_c.matricule)

    # ── Clear wipes session keys ──────────────────────────────────────
    def test_clear_removes_session_and_redirects_home(self):
        session = self.client.session
        session['kiosk_student_id'] = self.student_c.id
        session['kiosk_search_matches'] = [self.student_a.id]
        session.save()

        response = self.client.get('/public/kiosk/clear/')
        self.assertRedirects(response, '/public/kiosk/', fetch_redirect_response=False)
        self.assertNotIn('kiosk_student_id', self.client.session)
        self.assertNotIn('kiosk_search_matches', self.client.session)

    # ── Inactive student not matched ──────────────────────────────────
    def test_inactive_student_not_found_in_search(self):
        inactive = Student.objects.create(
            name="Inactive Child",
            parent_contact="0611112222",
            is_active=False,
        )
        response = self.client.post('/public/kiosk/search/', {
            'search_query': inactive.parent_contact,
        })
        self.assertRedirects(response, '/public/kiosk/', fetch_redirect_response=False)


class SystemSettingsTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.admin_user = User.objects.create_superuser('admin_settings', 'admin@example.com', 'password123')
        self.normal_user = User.objects.create_user('normal_user', 'user@example.com', 'password123')

    def test_get_setting_fallback_and_override(self):
        from core.utils import get_setting, set_setting
        # Initial setting seeded in migration or fallback
        val = get_setting('CURRENCY_SYMBOL', 'DH')
        self.assertEqual(val, 'DH')

        # Override via set_setting
        set_setting('CURRENCY_SYMBOL', 'MAD')
        self.assertEqual(get_setting('CURRENCY_SYMBOL'), 'MAD')

    def test_settings_view_access_control(self):
        # Anonymous redirect by AdminOnlyMiddleware
        res = self.client.get('/settings/')
        self.assertRedirects(res, '/admin/login/', fetch_redirect_response=False)

        # Normal non-staff user access denied by AdminOnlyMiddleware
        self.client.login(username='normal_user', password='password123')
        res = self.client.get('/settings/')
        self.assertRedirects(res, '/admin/login/', fetch_redirect_response=False)

        # Superuser / staff access allowed
        self.client.login(username='admin_settings', password='password123')
        res = self.client.get('/settings/')
        self.assertEqual(res.status_code, 200)


    def test_settings_view_post_updates_db(self):
        from core.utils import get_setting
        self.client.login(username='admin_settings', password='password123')
        post_data = {
            'SCHOOL_NAME': 'Nouveau Centre Edu',
            'SCHOOL_SUBTITLE': 'Excellence & Langues',
            'SCHOOL_ADDRESS': '123 Boulevard Hassan II',
            'SCHOOL_PHONE': '0600000000',
            'SCHOOL_EMAIL': 'info@edu.ma',
            'CURRENCY_SYMBOL': 'EUR',
            'ENABLE_PRORATION': 'on',
            'LATE_PAYMENT_GRACE_DAYS': 7,
            'RECEIPT_FOOTER_THANK_YOU': 'Merci de votre visite !',
            'WHATSAPP_SESSION_NOTIFICATIONS_ENABLED': 'on',
            'WHATSAPP_AUTO_ABSENCE_NOTIFICATIONS': 'on',
            'WHATSAPP_AUTO_GROUP_INVITE_ON_FIRST_PAYMENT': 'on',
            'KIOSK_TIMEOUT': 60,
            'KIOSK_SEARCH_ENABLED': 'on',
            'DEFAULT_TEACHER_PAYMENT_METHOD': 'HOURLY',
        }
        res = self.client.post('/settings/', post_data)
        self.assertRedirects(res, '/settings/')

        # Verify DB and get_setting reflect new values
        self.assertEqual(get_setting('SCHOOL_NAME'), 'Nouveau Centre Edu')
        self.assertEqual(get_setting('CURRENCY_SYMBOL'), 'EUR')
        self.assertEqual(get_setting('LATE_PAYMENT_GRACE_DAYS'), '7')
        self.assertEqual(get_setting('DEFAULT_TEACHER_PAYMENT_METHOD'), 'HOURLY')


class SchedulingAutoSaveAndHandlingTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username='admin_sched',
            password='password123',
            email='admin@test.com'
        )
        self.client.login(username='admin_sched', password='password123')

        self.room1 = Room.objects.create(name='Salle Alpha', capacity=15)
        self.room2 = Room.objects.create(name='Salle Beta', capacity=20)
        self.teacher = Teacher.objects.create(
            name='Professeur Principal',
            phone='0611223344',
            payment_method='PERCENTAGE',
            payment_percentage=Decimal('50.00')
        )
        self.sub_teacher = Teacher.objects.create(
            name='Professeur Remplaçant',
            phone='0655667788',
            payment_method='PERCENTAGE',
            payment_percentage=Decimal('50.00')
        )
        self.group = CourseGroup.objects.create(
            name='Groupe Physique',
            subject='Physique',
            monthly_price=Decimal('150.00'),
            teacher=self.teacher
        )
        self.student = Student.objects.create(
            name='Karim Alami',
            phone='0699887766',
            parent_contact='0688776655'
        )
        self.group.students.add(self.student)

        self.session = Session.objects.create(
            group=self.group,
            date=date(2026, 9, 10),
            start_time=time(10, 0),
            end_time=time(12, 0),
            room=self.room1,
            status='PLANNED'
        )

    def test_session_update_auto_saves_as_unhandled(self):
        """Updating a session via session_update_ajax saves changes and creates an unhandled change history record."""
        initial_unhandled = AuditService.get_unhandled_count()

        res = self.client.post(f'/sessions/{self.session.id}/update-ajax/', {
            'date': '2026-09-11',
            'start_time': '11:00',
            'end_time': '13:00',
            'room_id': self.room2.id,
            'scope': 'only_this',
            'change_reason': 'Déplacement test'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['unhandled_count'], initial_unhandled + 1)

        # Check DB was auto-saved
        self.session.refresh_from_db()
        self.assertEqual(self.session.date, date(2026, 9, 11))
        self.assertEqual(self.session.start_time, time(11, 0))
        self.assertEqual(self.session.room, self.room2)

        # Check unhandled history record exists
        unhandled = AuditService.get_unhandled_changes(session_id=self.session.id)
        self.assertEqual(unhandled.count(), 1)
        record = unhandled.first()
        self.assertFalse(record.is_handled)
        self.assertEqual(record.change_reason, 'Déplacement test')
        self.assertIn(self.session.id, AuditService.get_unhandled_session_ids())

    def test_schedule_unhandled_changes_ajax_endpoint(self):
        """schedule_unhandled_changes_ajax returns formatted unhandled changes."""
        AuditService.log_change(
            session=self.session,
            user=self.user,
            action='manual_override',
            previous_values={'date': '2026-09-10'},
            new_values={'date': '2026-09-12'},
            change_reason='Test reason',
            is_handled=False
        )

        res = self.client.get('/schedule/unhandled-changes-ajax/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertGreaterEqual(data['count'], 1)
        item = [c for c in data['changes'] if c['session_id'] == self.session.id][0]
        self.assertEqual(item['group_name'], 'Groupe Physique')
        self.assertEqual(item['change_reason'], 'Test reason')

    def test_handle_changes_send_all(self):
        """schedule_handle_changes_ajax with send_all marks all changes as handled and sets handled_at."""
        h1 = AuditService.log_change(
            session=self.session,
            user=self.user,
            action='manual_override',
            previous_values={'room': 'Salle Alpha'},
            new_values={'room': 'Salle Beta'},
            change_reason='Salle change',
            is_handled=False
        )

        res = self.client.post('/schedule/handle-changes-ajax/', {
            'action': 'send_all',
            'notify_teachers': '1',
            'notify_students': '1'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertGreaterEqual(data['handled_count'], 1)
        self.assertEqual(data['unhandled_count'], 0)

        h1.refresh_from_db()
        self.assertTrue(h1.is_handled)
        self.assertIsNotNone(h1.handled_at)

    def test_handle_changes_send_selected(self):
        """schedule_handle_changes_ajax with send_selected marks only the specified changes as handled."""
        h1 = AuditService.log_change(
            session=self.session,
            user=self.user,
            action='manual_override',
            previous_values={'room': 'Salle Alpha'},
            new_values={'room': 'Salle Beta'},
            is_handled=False
        )
        h2 = AuditService.log_change(
            session=self.session,
            user=self.user,
            action='manual_override',
            previous_values={'start_time': '10:00'},
            new_values={'start_time': '11:00'},
            is_handled=False
        )

        res = self.client.post('/schedule/handle-changes-ajax/', {
            'action': 'send_selected',
            'history_ids': str(h1.id),
            'notify_teachers': '1',
            'notify_students': '1'
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['handled_count'], 1)

        h1.refresh_from_db()
        h2.refresh_from_db()
        self.assertTrue(h1.is_handled)
        self.assertFalse(h2.is_handled)

    def test_handle_changes_mark_handled_silent(self):
        """schedule_handle_changes_ajax with mark_handled_silent marks changes as handled without notifications."""
        h1 = AuditService.log_change(
            session=self.session,
            user=self.user,
            action='manual_override',
            previous_values={'notes': ''},
            new_values={'notes': 'Silent note update'},
            is_handled=False
        )

        res = self.client.post('/schedule/handle-changes-ajax/', {
            'action': 'mark_handled_silent',
            'history_ids': str(h1.id)
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['notifications_sent'], 0)

        h1.refresh_from_db()
        self.assertTrue(h1.is_handled)
        self.assertIsNotNone(h1.handled_at)

    def test_whatsapp_schedule_notifications_page(self):
        """Test the dedicated WhatsApp schedule notifications page renders with context data."""
        AuditService.log_change(
            session=self.session,
            user=self.user,
            action='manual_override',
            previous_values={'room': 'Salle Alpha'},
            new_values={'room': 'Salle Beta'},
            change_reason='Salle change',
            is_handled=False
        )

        res = self.client.get('/whatsapp/schedule-notifications/')
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, 'core/whatsapp_schedule_notifications.html')
        self.assertGreaterEqual(res.context['unhandled_count'], 1)
        self.assertEqual(res.context['students_affected'], 1)
        self.assertEqual(res.context['teachers_affected'], 1)

    def test_whatsapp_dashboard_has_schedule_notifications_link(self):
        """Test the WhatsApp dashboard renders the Planning & Séances notifications card."""
        res = self.client.get('/whatsapp/')
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, '/whatsapp/schedule-notifications/')
        self.assertContains(res, 'Planning & Séances')

    def test_session_detail_ajax_today_is_editable(self):
        """Test that sessions on today's date return is_future=True and is_editable=True."""
        today_session = Session.objects.create(
            group=self.group,
            date=timezone.now().date(),
            start_time=time(14, 0),
            end_time=time(16, 0),
            room=self.room1,
            status='PLANNED'
        )
        res = self.client.get(f'/sessions/{today_session.id}/detail-ajax/')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['is_future'])
        self.assertTrue(data['is_editable'])
        self.assertTrue(data['is_today'])

    def test_whatsapp_absence_notifications_no_duplicates(self):
        """Test that a student with multiple phone numbers or records only appears once in absence notifications."""
        from core.models import Attendance
        self.student.parent_contact = '0612345678'
        self.student.parent_contact_2 = '0687654321'
        self.student.save()

        today = timezone.now().date()
        Attendance.objects.create(
            student=self.student,
            course_group=self.group,
            date=today,
            is_present=False
        )

        res = self.client.get(f'/whatsapp/absence-notifications/?date={today}')
        self.assertEqual(res.status_code, 200)
        contacts = res.context['absence_contacts']
        student_ids = [c['student'].id for c in contacts]
        # Verify student only appears once
        self.assertEqual(student_ids.count(self.student.id), 1)

    def test_whatsapp_send_ajax_auto_resolves_history(self):
        """Test that sending a message with history_id marks that history entry as handled."""
        from unittest.mock import patch
        h = AuditService.log_change(
            session=self.session,
            user=self.user,
            action='manual_override',
            previous_values={'room': 'Salle Alpha'},
            new_values={'room': 'Salle Beta'},
            is_handled=False
        )

        with patch('core.utils.WhatsAppServiceAPI.send_message', return_value={'success': True, 'messageId': 'test-123'}):
            res = self.client.post('/whatsapp/send/', {
                'phone': '0611223344',
                'message': 'Test change message',
                'message_type': 'session_reminder',
                'history_id': str(h.id)
            })
        self.assertEqual(res.status_code, 200)
        h.refresh_from_db()
        self.assertTrue(h.is_handled)
        self.assertIsNotNone(h.handled_at)


class WhatsAppGroupInviteTestCase(TestCase):
    def setUp(self):
        self.teacher = Teacher.objects.create(name="Professeur Test", phone="0611223344")
        self.group1 = CourseGroup.objects.create(
            name="Groupe Math BAC",
            subject="Mathématiques",
            monthly_price=Decimal("400.00"),
            teacher=self.teacher,
            whatsapp_group_link="https://chat.whatsapp.com/TEST_MATH_GROUP",
            is_active=True
        )
        self.group2 = CourseGroup.objects.create(
            name="Groupe Physique BAC",
            subject="Physique",
            monthly_price=Decimal("400.00"),
            teacher=self.teacher,
            whatsapp_group_link="",
            is_active=True
        )
        self.student = Student.objects.create(
            name="Youssef Alami",
            parent_name="Ahmed Alami",
            parent_contact="0661234567",
            phone="0671234567"
        )
        from core.utils import set_setting
        set_setting('WHATSAPP_AUTO_GROUP_INVITE_ON_FIRST_PAYMENT', 'True')

    def test_auto_send_group_invite_on_first_payment(self):
        from unittest.mock import patch
        from core.models import Enrollment, Payment, WhatsAppSendLog

        enrollment1 = Enrollment.objects.create(student=self.student, course_group=self.group1)
        enrollment2 = Enrollment.objects.create(student=self.student, course_group=self.group2)

        self.assertFalse(enrollment1.whatsapp_invite_sent)
        self.assertFalse(enrollment2.whatsapp_invite_sent)

        # First payment
        with patch('core.utils.WhatsAppServiceAPI.send_message', return_value={'success': True, 'messageId': 'msg-101'}) as mock_send:
            payment1 = Payment.objects.create(
                student=self.student,
                amount=Decimal("400.00"),
                payment_date=timezone.now().date(),
                month_covered=timezone.now().date().replace(day=1),
                status='PAID'
            )

            # Check that send_message was called with the group link
            self.assertTrue(mock_send.called)
            called_args, called_kwargs = mock_send.call_args
            called_message = called_args[1] if len(called_args) > 1 else called_kwargs.get('message', '')
            self.assertIn("https://chat.whatsapp.com/TEST_MATH_GROUP", called_message)
            self.assertIn("Groupe Math BAC", called_message)

        enrollment1.refresh_from_db()
        self.assertTrue(enrollment1.whatsapp_invite_sent)

        # Verify WhatsAppSendLog was created with message_type 'group_invite'
        log = WhatsAppSendLog.objects.filter(student=self.student, message_type='group_invite').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, 'SENT')

        # Second payment should NOT re-send WhatsApp group invites
        with patch('core.utils.WhatsAppServiceAPI.send_message', return_value={'success': True, 'messageId': 'msg-102'}) as mock_send_2:
            payment2 = Payment.objects.create(
                student=self.student,
                amount=Decimal("400.00"),
                payment_date=timezone.now().date(),
                month_covered=timezone.now().date().replace(day=1),
                status='PAID'
            )
            mock_send_2.assert_not_called()

    def test_group_without_invite_link_skipped(self):
        from unittest.mock import patch
        from core.models import Enrollment, Payment

        # Enroll only in group2 (which has no whatsapp_group_link)
        enrollment = Enrollment.objects.create(student=self.student, course_group=self.group2)

        with patch('core.utils.WhatsAppServiceAPI.send_message', return_value={'success': True}) as mock_send:
            Payment.objects.create(
                student=self.student,
                amount=Decimal("400.00"),
                payment_date=timezone.now().date(),
                month_covered=timezone.now().date().replace(day=1),
                status='PAID'
            )
            mock_send.assert_not_called()

    def test_disabled_setting_skips_invite(self):
        from unittest.mock import patch
        from core.models import Enrollment, Payment, SystemSetting

        SystemSetting.objects.update_or_create(
            key='WHATSAPP_AUTO_GROUP_INVITE_ON_FIRST_PAYMENT',
            defaults={'value': 'False'}
        )
        from django.core.cache import cache
        cache.delete('sys_setting_WHATSAPP_AUTO_GROUP_INVITE_ON_FIRST_PAYMENT')

        Enrollment.objects.create(student=self.student, course_group=self.group1)

        with patch('core.utils.WhatsAppServiceAPI.send_message', return_value={'success': True}) as mock_send:
            Payment.objects.create(
                student=self.student,
                amount=Decimal("400.00"),
                payment_date=timezone.now().date(),
                month_covered=timezone.now().date().replace(day=1),
                status='PAID'
            )
            mock_send.assert_not_called()







