from django.test import TestCase
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key

from model_bakery import baker

from evap.evaluation.tests.tools import WebTest
from evap.evaluation.models import Contribution, Course, Evaluation, UserProfile
from evap.rewards.models import RewardPointGranting, RewardPointRedemption
from evap.staff.tools import merge_users, delete_navbar_cache_for_users, remove_user_from_represented_and_ccing_users


class NavbarCacheTest(WebTest):
    def test_navbar_cache_deletion_for_users(self):
        user1 = baker.make(UserProfile, email="user1@institution.example.com")
        user2 = baker.make(UserProfile, email="user2@institution.example.com")

        # create navbar caches for anonymous user, user1 and user2
        self.app.get("/")
        self.app.get("/results/", user="user1@institution.example.com")
        self.app.get("/results/", user="user2@institution.example.com")

        cache_key1 = make_template_fragment_key("navbar", [user1.email, "en"])
        cache_key2 = make_template_fragment_key("navbar", [user2.email, "en"])
        cache_key_anonymous = make_template_fragment_key("navbar", ["", "en"])

        self.assertIsNotNone(cache.get(cache_key1))
        self.assertIsNotNone(cache.get(cache_key2))
        self.assertIsNotNone(cache.get(cache_key_anonymous))

        delete_navbar_cache_for_users([user2])

        self.assertIsNotNone(cache.get(cache_key1))
        self.assertIsNone(cache.get(cache_key2))
        self.assertIsNotNone(cache.get(cache_key_anonymous))


class MergeUsersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user1 = baker.make(UserProfile, email="test1@institution.example.com")
        cls.user2 = baker.make(UserProfile, email="test2@institution.example.com")
        cls.user3 = baker.make(UserProfile, email="test3@institution.example.com")
        cls.group1 = baker.make(Group, pk=4)
        cls.group2 = baker.make(Group, pk=5)
        cls.main_user = baker.make(
            UserProfile,
            title="Dr.",
            first_name="Main",
            last_name="",
            email=None,  # test that merging works when taking the email from other user (UniqueConstraint)
            groups=[cls.group1],
            delegates=[cls.user1, cls.user2],
            represented_users=[cls.user3],
            cc_users=[cls.user1],
            ccing_users=[],
        )
        cls.other_user = baker.make(
            UserProfile,
            title="",
            first_name="Other",
            last_name="User",
            email="other@test.com",
            groups=[cls.group2],
            delegates=[cls.user3],
            represented_users=[cls.user1],
            cc_users=[],
            ccing_users=[cls.user1, cls.user2],
            is_superuser=True,
        )
        cls.course1 = baker.make(Course, responsibles=[cls.main_user])
        cls.course2 = baker.make(Course, responsibles=[cls.main_user])
        cls.course3 = baker.make(Course, responsibles=[cls.other_user])
        cls.evaluation1 = baker.make(
            Evaluation, course=cls.course1, name_de="evaluation1", participants=[cls.main_user, cls.other_user]
        )  # this should make the merge fail
        cls.evaluation2 = baker.make(
            Evaluation, course=cls.course2, name_de="evaluation2", participants=[cls.main_user], voters=[cls.main_user]
        )
        cls.evaluation3 = baker.make(
            Evaluation,
            course=cls.course3,
            name_de="evaluation3",
            participants=[cls.other_user],
            voters=[cls.other_user],
        )
        cls.contribution1 = baker.make(Contribution, contributor=cls.main_user, evaluation=cls.evaluation1)
        cls.contribution2 = baker.make(
            Contribution, contributor=cls.other_user, evaluation=cls.evaluation1
        )  # this should make the merge fail
        cls.contribution3 = baker.make(Contribution, contributor=cls.other_user, evaluation=cls.evaluation2)
        cls.rewardpointgranting_main = baker.make(RewardPointGranting, user_profile=cls.main_user)
        cls.rewardpointgranting_other = baker.make(RewardPointGranting, user_profile=cls.other_user)
        cls.rewardpointredemption_main = baker.make(RewardPointRedemption, user_profile=cls.main_user)
        cls.rewardpointredemption_other = baker.make(RewardPointRedemption, user_profile=cls.other_user)

    def setUp(self):
        # merge users changes these instances in such a way that refresh_from_db doesn't work anymore.
        self.main_user = UserProfile.objects.get(first_name="Main", last_name="")
        self.other_user = UserProfile.objects.get(email="other@test.com")

    def test_merge_handles_all_attributes(self):
        user1 = baker.make(UserProfile)
        user2 = baker.make(UserProfile)

        all_attrs = list(field.name for field in UserProfile._meta.get_fields(include_hidden=True))

        # these are relations to intermediate models generated by django for m2m relations.
        # we can safely ignore these since the "normal" fields of the m2m relations are present as well.
        all_attrs = list(attr for attr in all_attrs if not attr.startswith("UserProfile_"))

        # equally named fields are not supported, sorry
        self.assertEqual(len(all_attrs), len(set(all_attrs)))

        # some attributes we don't care about when merging
        ignored_attrs = {
            "id",  # nothing to merge here
            "password",  # not used in production
            "last_login",  # something to really not care about
            "user_permissions",  # we don't use permissions
            "logentry",  # wtf
            "login_key",  # we decided to discard other_user's login key
            "login_key_valid_until",  # not worth dealing with
            "language",  # Not worth dealing with
            "Evaluation_voters+",  # some more intermediate models, for an explanation see above
            "Evaluation_participants+",  # intermediate model
        }
        expected_attrs = set(all_attrs) - ignored_attrs

        # actual merge happens here
        merged_user, errors, warnings = merge_users(user1, user2)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        handled_attrs = set(merged_user.keys())

        # attributes that are handled in the merge method but that are not present in the merged_user dict
        # add attributes here only if you're actually dealing with them in merge_users().
        additional_handled_attrs = {
            "grades_last_modified_user+",
            "Course_responsibles+",
        }

        actual_attrs = handled_attrs | additional_handled_attrs

        self.assertEqual(expected_attrs, actual_attrs)

    def test_merge_users_does_not_change_data_on_fail(self):
        __, errors, warnings = merge_users(self.main_user, self.other_user)  # merge should fail
        self.assertCountEqual(errors, ["contributions", "evaluations_participating_in"])
        self.assertCountEqual(warnings, ["rewards"])

        # assert that nothing has changed
        self.main_user.refresh_from_db()
        self.other_user.refresh_from_db()

        self.assertEqual(self.main_user.title, "Dr.")
        self.assertEqual(self.main_user.first_name, "Main")
        self.assertEqual(self.main_user.last_name, "")
        self.assertEqual(self.main_user.email, None)
        self.assertFalse(self.main_user.is_superuser)
        self.assertEqual(set(self.main_user.groups.all()), {self.group1})
        self.assertEqual(set(self.main_user.delegates.all()), {self.user1, self.user2})
        self.assertEqual(set(self.main_user.represented_users.all()), {self.user3})
        self.assertEqual(set(self.main_user.cc_users.all()), {self.user1})
        self.assertEqual(set(self.main_user.ccing_users.all()), set())
        self.assertTrue(RewardPointGranting.objects.filter(user_profile=self.main_user).exists())
        self.assertTrue(RewardPointRedemption.objects.filter(user_profile=self.main_user).exists())

        self.assertEqual(self.other_user.title, "")
        self.assertEqual(self.other_user.first_name, "Other")
        self.assertEqual(self.other_user.last_name, "User")
        self.assertEqual(self.other_user.email, "other@test.com")
        self.assertEqual(set(self.other_user.groups.all()), {self.group2})
        self.assertEqual(set(self.other_user.delegates.all()), {self.user3})
        self.assertEqual(set(self.other_user.represented_users.all()), {self.user1})
        self.assertEqual(set(self.other_user.cc_users.all()), set())
        self.assertEqual(set(self.other_user.ccing_users.all()), {self.user1, self.user2})
        self.assertTrue(RewardPointGranting.objects.filter(user_profile=self.other_user).exists())
        self.assertTrue(RewardPointRedemption.objects.filter(user_profile=self.other_user).exists())

        self.assertEqual(set(self.course1.responsibles.all()), {self.main_user})
        self.assertEqual(set(self.course2.responsibles.all()), {self.main_user})
        self.assertEqual(set(self.course3.responsibles.all()), {self.other_user})
        self.assertEqual(set(self.evaluation1.participants.all()), {self.main_user, self.other_user})
        self.assertEqual(set(self.evaluation1.participants.all()), {self.main_user, self.other_user})
        self.assertEqual(set(self.evaluation2.participants.all()), {self.main_user})
        self.assertEqual(set(self.evaluation2.voters.all()), {self.main_user})
        self.assertEqual(set(self.evaluation3.participants.all()), {self.other_user})
        self.assertEqual(set(self.evaluation3.voters.all()), {self.other_user})

    def test_merge_users_changes_data_on_success(self):
        # Fix data so that the merge will not fail as in test_merge_users_does_not_change_data_on_fail
        self.evaluation1.participants.set([self.main_user])
        self.contribution2.delete()

        __, errors, warnings = merge_users(self.main_user, self.other_user)  # merge should succeed
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["rewards"])  # rewards warning is still there

        self.main_user.refresh_from_db()

        self.assertEqual(self.main_user.title, "Dr.")
        self.assertEqual(self.main_user.first_name, "Main")
        self.assertEqual(self.main_user.last_name, "User")
        self.assertEqual(self.main_user.email, "other@test.com")
        self.assertTrue(self.main_user.is_superuser)
        self.assertEqual(set(self.main_user.groups.all()), {self.group1, self.group2})
        self.assertEqual(set(self.main_user.delegates.all()), {self.user1, self.user2, self.user3})
        self.assertEqual(set(self.main_user.represented_users.all()), {self.user1, self.user3})
        self.assertEqual(set(self.main_user.cc_users.all()), {self.user1})
        self.assertEqual(set(self.main_user.ccing_users.all()), {self.user1, self.user2})
        self.assertTrue(RewardPointGranting.objects.filter(user_profile=self.main_user).exists())
        self.assertTrue(RewardPointRedemption.objects.filter(user_profile=self.main_user).exists())

        self.assertEqual(set(self.course1.responsibles.all()), {self.main_user})
        self.assertEqual(set(self.course2.responsibles.all()), {self.main_user})
        self.assertEqual(set(self.course2.responsibles.all()), {self.main_user})
        self.assertEqual(set(self.evaluation1.participants.all()), {self.main_user})
        self.assertEqual(set(self.evaluation2.participants.all()), {self.main_user})
        self.assertEqual(set(self.evaluation2.voters.all()), {self.main_user})
        self.assertEqual(set(self.evaluation3.participants.all()), {self.main_user})
        self.assertEqual(set(self.evaluation3.voters.all()), {self.main_user})

        self.assertFalse(UserProfile.objects.filter(email="other_user@institution.example.com").exists())
        self.assertFalse(RewardPointGranting.objects.filter(user_profile=self.other_user).exists())
        self.assertFalse(RewardPointRedemption.objects.filter(user_profile=self.other_user).exists())


class RemoveUserFromRepresentedAndCCingUsersTest(TestCase):
    def test_remove_user_from_represented_and_ccing_users(self):
        delete_user = baker.make(UserProfile)
        delete_user2 = baker.make(UserProfile)
        user1 = baker.make(UserProfile, delegates=[delete_user, delete_user2], cc_users=[delete_user])
        user2 = baker.make(UserProfile, delegates=[delete_user], cc_users=[delete_user, delete_user2])

        messages = remove_user_from_represented_and_ccing_users(delete_user)
        self.assertEqual([set(user1.delegates.all()), set(user1.cc_users.all())], [{delete_user2}, set()])
        self.assertEqual([set(user2.delegates.all()), set(user2.cc_users.all())], [set(), {delete_user2}])
        self.assertEqual(len(messages), 4)

        messages2 = remove_user_from_represented_and_ccing_users(delete_user2)
        self.assertEqual([set(user1.delegates.all()), set(user1.cc_users.all())], [set(), set()])
        self.assertEqual([set(user2.delegates.all()), set(user2.cc_users.all())], [set(), set()])
        self.assertEqual(len(messages2), 2)

    def test_do_not_remove_from_ignored_users(self):
        delete_user = baker.make(UserProfile)
        user1 = baker.make(UserProfile, delegates=[delete_user], cc_users=[delete_user])
        user2 = baker.make(UserProfile, delegates=[delete_user], cc_users=[delete_user])

        messages = remove_user_from_represented_and_ccing_users(delete_user, [user2])
        self.assertEqual([set(user1.delegates.all()), set(user1.cc_users.all())], [set(), set()])
        self.assertEqual([set(user2.delegates.all()), set(user2.cc_users.all())], [{delete_user}, {delete_user}])
        self.assertEqual(len(messages), 2)

    def test_do_nothing_if_test_run(self):
        delete_user = baker.make(UserProfile)
        user1 = baker.make(UserProfile, delegates=[delete_user], cc_users=[delete_user])
        user2 = baker.make(UserProfile, delegates=[delete_user], cc_users=[delete_user])

        messages = remove_user_from_represented_and_ccing_users(delete_user, test_run=True)
        self.assertEqual([set(user1.delegates.all()), set(user1.cc_users.all())], [{delete_user}, {delete_user}])
        self.assertEqual([set(user2.delegates.all()), set(user2.cc_users.all())], [{delete_user}, {delete_user}])
        self.assertEqual(len(messages), 4)
