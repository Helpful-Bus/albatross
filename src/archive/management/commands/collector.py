import os
import pytz
import signal
import sys
import time
import traceback
import tweepy

from datetime import datetime

from django.core.management.base import BaseCommand
from django.db.models.query_utils import Q

from allauth.socialaccount.models import SocialApp, SocialToken

from users.models import User

from ..listeners import StreamArchiver
from ..mixins import NotificationMixin
from ...models import Archive


class Command(NotificationMixin, BaseCommand):
    """
    Loop forever checking the db for when to start/stop an archive.  New streams
    are stored in self.streams, keyed by the user owning the stream.
    """

    LOOP_TIME = 1
    CHILL_TIME = 30  # 420: Enhance your calm
    LISTENER_WAIT_TIME = 5  # Time to wait between starting listeners

    VERBOSITY_FILE = "/tmp/tweetpile-collector.verbosity"

    def __init__(self):
        BaseCommand.__init__(self)
        self.tracking = []
        self.streams = {}
        self.first_pass_completed = False
        self.verbosity = 1

        self.socialapp = SocialApp.objects.get(pk=1)

    def handle(self, *args, **options):

        self.verbosity = options.get("verbosity", self.verbosity)

        if self.verbosity > 0:
            sys.stdout.write("Starting Collector\n")

        signal.signal(signal.SIGINT, self.exit)
        signal.signal(signal.SIGTERM, self.exit)

        try:
            self.loop()
        except Exception as e:
            sys.stdout.write("Exception: {}".format(e))
            sys.stdout.write(traceback.format_exc())
            self.exit()

    def exit(self, *args):
        if self.verbosity > 0:
            sys.stdout.write("Exiting\n")
        for user, stream in self.streams.items():
            if self.verbosity > 1:
                sys.stdout.write("  Killing stream for {}: ".format(user))
            stream.disconnect()
            stream.listener.close_log()
            if self.verbosity > 1:
                sys.stdout.write("[ DONE ]\n".format(user))
        sys.exit(0)

    def _verbosity_check(self):

        if not os.path.exists(self.VERBOSITY_FILE):
            return

        if not os.access(self.VERBOSITY_FILE, os.W_OK):
            print("Verbosity change failure: permissions are wrong")
            return

        with open(self.VERBOSITY_FILE) as f:

            try:
                verbosity = int(f.read().strip())
                if verbosity not in (1, 2, 3):
                    raise ValueError
            except ValueError:
                print("Verbosity change failed.  Check that file.")
                return

            print("Setting listener verbosity to {}".format(verbosity))

            self.verbosity = verbosity
            for user in self.streams:
                self.streams[user].listener.set_verbosity(self.verbosity)

        os.unlink(self.VERBOSITY_FILE)

    def loop(self):

        while True:

            self._verbosity_check()

            now = datetime.now(tz=pytz.UTC)

            to_start = self._get_archives_to_start(now)
            to_stop = Archive.objects.filter(stopped__lte=now, is_running=True)

            if to_start or to_stop:
                self.adjust_connections(to_start, to_stop)

            sys.stdout.flush()

            time.sleep(self.LOOP_TIME)

    def start_tracking(self, archive):
        if archive not in self.tracking:
            self.tracking.append(archive)
        archive.is_running = True
        archive.save(update_fields=("is_running",))

    def stop_tracking(self, archive):
        if archive in self.tracking:
            self.tracking.remove(archive)
        archive.is_running = False
        archive.save(update_fields=("is_running",))

    def adjust_connections(self, to_start, to_stop):

        if self.verbosity > 1:
            sys.stdout.write(
                "Adjusting connections: {}\n".format(self.tracking)
            )

        users_adjusting = [a.user for a in list(to_start) + list(to_stop)]

        # Kill streams belonging to users that are either stopping or starting
        # a new collection.
        for user in users_adjusting:
            if user in self.streams:
                self.streams[user].disconnect()
                self.streams[user].listener.close_log()
                del(self.streams[user])

        for archive in to_stop:
            self.stop_tracking(archive)

        for archive in to_start:
            self.start_tracking(archive)

        # Regroup the archives so we only have one stream per user.
        groups = {}
        for archive in self.tracking:
            if archive.user in users_adjusting:
                if archive.user not in groups:
                    groups[archive.user] = []
                groups[archive.user].append(archive)

        for user, archives in groups.items():
            if self.verbosity > 1:
                sys.stdout.write("Connecting: {}::{}\n".format(user, archives))
            try:
                api = self._authenticate(user)
                self.streams[user] = tweepy.Stream(
                    auth=api.auth,
                    listener=StreamArchiver(
                        archives, api=api, verbosity=self.verbosity)
                )
                self.streams[user].filter(
                    track=set([a.query for a in archives]),
                    async=True
                )
            except Exception as e:
                self._alert("Tweetpile collector exception [collector]", e)

            time.sleep(self.LISTENER_WAIT_TIME)

    def _get_archives_to_start(self, now):
        """
        If the archiver is killed unexpectedly, we need to account for the
        special case of its "first pass", where we need to re-start should-be
        ongoing archivals.  We also call ._handle_restarts() here to capture the
        special case where the stream was killed for whatever reason.
        """

        r = Archive.objects\
            .filter(started__lte=now)\
            .exclude(pk__in=[a.pk for a in self.tracking])

        if self.first_pass_completed:
            r = r.exclude(is_running=False)
            self.first_pass_completed = True

        return self._handle_restarts(
            r.filter(Q(stopped__gt=now) | Q(stopped__isnull=True))
        ).exclude(
            user__status=User.STATUS_DISABLED
        )

    def _handle_restarts(self, to_start):
        """
        For when a stream spontaneously disconnects (errors, Twitter whim)
        """

        to_restart = []
        for user, stream in self.streams.items():
            if not stream.running:
                sys.stdout.write("Reconnection required: {}\n".format(user))
                for channel in stream.listener.channels:
                    to_restart.append(channel["archive"].pk)

        if not to_restart:
            return to_start

        time.sleep(self.CHILL_TIME)

        return Archive.objects.filter(
            pk__in=[a.pk for a in to_start] + to_restart)

    def _authenticate(self, user):

        socialtoken = SocialToken.objects.get(account__user=user)

        access = {
            "key": socialtoken.token,
            "secret": socialtoken.token_secret
        }
        consumer = {
            "key": self.socialapp.client_id,
            "secret": self.socialapp.secret
        }

        auth = tweepy.OAuthHandler(consumer["key"], consumer["secret"])
        auth.set_access_token(access["key"], access["secret"])

        return tweepy.API(auth)
