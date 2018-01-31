import logging
import threading

import greenstalk

from freezing.model.msg.mq import ActivityUpdate, ActivityUpdateSchema
from freezing.model import meta
from freezing.model.msg.strava import AspectType

from freezing.sync.autolog import log
from freezing.sync.data.activity import ActivitySync
from freezing.sync.data.streams import StreamSync


class ActivityUpdateSubscriber:

    def __init__(self, beanstalk_client: greenstalk.Client, shutdown_event: threading.Event):
        self.client = beanstalk_client
        self.shutdown_event = shutdown_event
        self.logger = logging.getLogger(__name__)
        self.activity_sync = ActivitySync(self.logger)
        self.streams_sync = StreamSync(self.logger)

    def handle_message(self, message: ActivityUpdate):
        self.logger.info("Processing activity update {}".format(message))

        if message.operation is AspectType.delete:
            self.activity_sync.delete_activity(athlete_id=message.athlete_id,
                                               activity_id=message.activity_id)

        elif message.operation is AspectType.update:
            self.activity_sync.fetch_and_store_actvitiy_detail(athlete_id=message.athlete_id,
                                                               activity_id=message.activity_id)
            # (We'll assume the stream doens't need re-fetching.)

        elif message.operation is AspectType.create:
            self.activity_sync.fetch_and_store_actvitiy_detail(athlete_id=message.athlete_id,
                                                               activity_id=message.activity_id)
            self.streams_sync.fetch_and_store_activity_streams(athlete_id=message.athlete_id,
                                                               activity_id=message.activity_id)

    def run_forever(self):
        # This is expecting to run in the main thread. Needs a bit of redesign
        # if this is to be moved to a background thread.
        try:
            schema = ActivityUpdateSchema()

            while not self.shutdown_event.is_set():
                try:
                    job = self.client.reserve(timeout=30)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except greenstalk.TimedOutError:
                    self.logger.debug("Internal beanstalkd connection timeout; reconnecting.")
                    continue
                else:
                    try:
                        self.logger.info("Received message: {!r}".format(job.body))
                        update = schema.loads(job.body).data
                        self.handle_message(update)
                    except:
                        self.logger.exception("Error procesing message, will requeue w/ delay.")
                        self.client.release(job, delay=20)  # We put it back with a delay
                    else:
                        self.client.delete(job)
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.logger.exception("Unhandled error in tube subscriber loop, exiting.")
            self.shutdown_event.set()
            raise
