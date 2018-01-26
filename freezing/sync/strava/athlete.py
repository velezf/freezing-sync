import logging
import re
from typing import List
from datetime import datetime

import arrow
from geoalchemy import WKTSpatialElement

from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from freezing.sync.utils.cache import CachingActivityFetcher
from stravalib import unithelper

from stravalib import model as sm
from stravalib.unithelper import timedelta_to_seconds

from freezing.model import meta
from freezing.model.orm import Athlete, Ride, RideEffort, RidePhoto, RideError, RideGeo

from freezing.sync.config import config
from freezing.sync.exc import DataEntryError, CommandError, InvalidAuthorizationToken

from . import StravaClientForAthlete, BaseSync


class ActivitySync(BaseSync):

    def sync_athletes(self):

        sess = meta.session_factory()

        # We iterate over all of our athletes that have access tokens.  (We can't fetch anything
        # for those that don't.)

        q = sess.query(model.Athlete)
        q = q.filter(model.Athlete.access_token != None)

        for athlete in q.all():
            self.logger.info("Updating athlete: {0}".format(athlete))
            c = data.StravaClientForAthlete(athlete)
            try:
                strava_athlete = c.get_athlete()
                self.register_athlete(strava_athlete, athlete.access_token)
                self.register_athlete_team(strava_athlete, athlete)
            except:
                self.logger.warning("Error registering athlete {0}".format(athlete), exc_info=True)
                # But carry on

        self.disambiguate_athlete_display_names()

    def register_athlete(self, strava_athlete:sm.Athlete, access_token:str):
        """
        Ensure specified athlete is added to database, returns athlete model.

        :return: The added athlete model object.
        :rtype: :class:`bafs.model.Athlete`
        """
        athlete = db.session.query(Athlete).get(strava_athlete.id)
        if athlete is None:
            athlete = Athlete()
        athlete.id = strava_athlete.id
        athlete.name = '{0} {1}'.format(strava_athlete.firstname, strava_athlete.lastname).strip()
        # Temporary; we will update this in disambiguation phase.  (This isn't optimal; needs to be
        # refactored....)
        athlete.display_name = strava_athlete.firstname
        athlete.profile_photo = strava_athlete.profile

        athlete.access_token = access_token
        session.add(athlete)
        # We really shouldn't be committing here, since we want to disambiguate names after registering


        return athlete


    def disambiguate_athlete_display_names(self):
        q = db.session.query(model.Athlete)
        q = q.filter(model.Athlete.access_token != None)
        athletes = q.all()

        # Ok, here is the plan; bin these things together based on firstname and last initial.
        # Then iterate over each bin and if there are multiple entries, find the least number
        # of letters to make them all different. (we should be able to use set intersection
        # to check for differences within the bins?)

        def firstlast(name):
            name_parts = a.name.split(' ')
            fname = name_parts[0]
            if len(name_parts) < 2:
                lname = None
            else:
                lname = name_parts[-1]
            return (fname, lname)

        athletes_bin = {}
        for a in athletes:
            (fname, lname) = firstlast(a.name)
            if lname is None:
                # We only care about people with first and last names for this exercise
                # key = fname
                continue
            else:
                key = '{0} {1}'.format(fname, lname[0])
            athletes_bin.setdefault(key, []).append(a)

        for (name_key, athletes) in athletes_bin.items():
            shortest_lname = min([firstlast(a.name)[1] for a in athletes], key=len)
            required_length = None
            for i in range(len(shortest_lname)):
                # Calculate fname + lname-of-x-chars for each athlete.
                # If unique, then use this number and update the model objects
                candidate_short_lasts = [firstlast(a.name)[1][:i + 1] for a in athletes]
                if len(set(candidate_short_lasts)) == len(candidate_short_lasts):
                    required_length = i + 1
                    break

            if required_length is not None:
                for a in athletes:
                    fname, lname = firstlast(a.name)
                    log.debug("Converting '{fname} {lname}' -> '{fname} {minlname}".format(fname=fname,
                                                                                           lname=lname,
                                                                                           minlname=lname[
                                                                                                    :required_length]))
                    a.display_name = '{0} {1}'.format(fname, lname[:required_length])
            else:
                log.debug("Unable to find a minimum lastname; using full lastname.")
                # Just use full names
                for a in athletes:
                    fname, lname = firstlast(a.name)
                    a.display_name = '{0} {1}'.format(fname, lname[:required_length])

    def register_athlete_team(self, strava_athlete:sm.Athlete, athlete_model:Athlete) -> Team:
        """
        Updates db with configured team that matches the athlete's teams.

        Updates the passed-in Athlete model object with created/updated team.

        :param strava_athlete: The Strava model object for the athlete.
        :param athlete_model: The athlete model object.
        :return: The :class:`bafs.model.Team` object will be returned which matches
                 configured teams.
        :raise MultipleTeamsError: If this athlete is registered for multiple of
                                   the configured teams.  That won't work.
        :raise NoTeamsError: If no teams match.
        """
        log.info("Checking {0!r} against {1!r}".format(strava_athlete.clubs, app.config['BAFS_TEAMS']))
    
        matches = [c for c in strava_athlete.clubs if c.id in app.config['BAFS_TEAMS']]
        log.debug("Matched: {0!r}".format(matches))
        athlete_model.team = None
        if len(matches) > 1:
            log.info("Multiple teams matcheed.")
            raise MultipleTeamsError(matches)
        elif len(matches) == 0:
            raise NoTeamsError()
        else:
            club = matches[0]
            # create the team row if it does not exist
            team = db.session.query(Team).get(club.id)
            if team is None:
                team = Team()
            team.id = club.id
            team.name = club.name
            athlete_model.team = team
            db.session.add(team)
            return team