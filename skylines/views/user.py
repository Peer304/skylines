from datetime import date, timedelta

from flask import Blueprint, request, render_template, redirect, url_for, abort, g, flash
from flask.ext.login import login_required

from sqlalchemy.sql.expression import and_, or_
from sqlalchemy import func

from skylines import db
from skylines.forms import (
    ChangePasswordForm, ChangeClubForm, CreateClubForm, EditPilotForm
)
from skylines.lib.dbutil import get_requested_record
from skylines.model import (
    User, Club, Flight, Follower, Location, IGCFile, Notification, Event
)
from skylines.model.event import (
    create_follower_notification, create_club_join_event
)
#from skylines.views.users import recover_user_password

user_blueprint = Blueprint('user', 'skylines')


@user_blueprint.url_value_preprocessor
def _pull_user_id(endpoint, values):
    g.user_id = values.pop('user_id')
    g.user = get_requested_record(User, g.user_id)


@user_blueprint.url_defaults
def _add_user_id(endpoint, values):
    if hasattr(g, 'user_id'):
        values.setdefault('user_id', g.user_id)


def _get_distance_flight(distance):
    return Flight.query() \
        .filter(Flight.pilot == g.user) \
        .filter(Flight.olc_classic_distance >= distance) \
        .order_by(Flight.landing_time) \
        .first()


def _get_distance_flights():
    distance_flights = []

    largest_flight = g.user.get_largest_flights().first()
    if largest_flight:
        distance_flights.append([largest_flight.olc_classic_distance,
                                 largest_flight])

    for distance in [50000, 100000, 300000, 500000, 700000, 1000000]:
        distance_flight = _get_distance_flight(distance)
        if distance_flight is not None:
            distance_flights.append([distance, distance_flight])

    distance_flights.sort()
    return distance_flights


def _get_last_year_statistics():
    query = db.session.query(func.count('*').label('flights'),
                             func.sum(Flight.olc_classic_distance).label('distance'),
                             func.sum(Flight.duration).label('duration')) \
                      .filter(Flight.pilot == g.user) \
                      .filter(Flight.date_local > (date.today() - timedelta(days=365))) \
                      .first()

    last_year_statistics = dict(flights=0,
                                distance=0,
                                duration=timedelta(0),
                                speed=0)

    if query and query.flights > 0:
        duration_seconds = query.duration.days * 24 * 3600 + query.duration.seconds

        if duration_seconds > 0:
            last_year_statistics['speed'] = float(query.distance) / duration_seconds

        last_year_statistics['flights'] = query.flights
        last_year_statistics['distance'] = query.distance
        last_year_statistics['duration'] = query.duration

        last_year_statistics['average_distance'] = query.distance / query.flights
        last_year_statistics['average_duration'] = query.duration / query.flights

    return last_year_statistics


def _get_takeoff_locations():
    return Location.get_clustered_locations(Flight.takeoff_location_wkt,
                                            filter=(Flight.pilot == g.user))


def mark_user_notifications_read(user):
    if not g.current_user:
        return

    def add_user_filter(query):
        return query.filter(Event.actor_id == user.id)

    Notification.mark_all_read(g.current_user, filter_func=add_user_filter)
    db.session.commit()


@user_blueprint.route('/')
def index():
    mark_user_notifications_read(g.user)

    return render_template(
        'users/view.jinja',
        active_page='settings', user=g.user,
        distance_flights=_get_distance_flights(),
        takeoff_locations=_get_takeoff_locations(),
        last_year_statistics=_get_last_year_statistics())


@user_blueprint.route('/change_password', methods=['GET', 'POST'])
def change_password():
    if not g.user.is_writable(g.current_user):
        abort(403)

    form = ChangePasswordForm()
    if form.validate_on_submit():
        return change_password_post(form)

    return render_template('users/change_password.jinja', form=form)


def change_password_post(form):
    g.user.password = form.password.data
    g.user.recover_key = None

    db.session.commit()

    return redirect(url_for('.index'))


@user_blueprint.route('/edit', methods=['GET', 'POST'])
def edit():
    if not g.user.is_writable(g.current_user):
        abort(403)

    form = EditPilotForm(obj=g.user)
    if form.validate_on_submit():
        return edit_post(form)

    return render_template('users/edit.jinja', form=form)


def edit_post(form):
    g.user.email_address = form.email_address.data
    g.user.first_name = form.first_name.data
    g.user.last_name = form.last_name.data
    g.user.tracking_delay = request.form.get('tracking_delay', 0)

    unit_preset = request.form.get('unit_preset', 1, type=int)
    if unit_preset == 0:
        g.user.distance_unit = request.form.get('distance_unit', 1, type=int)
        g.user.speed_unit = request.form.get('speed_unit', 1, type=int)
        g.user.lift_unit = request.form.get('lift_unit', 0, type=int)
        g.user.altitude_unit = request.form.get('altitude_unit', 0, type=int)
    else:
        g.user.unit_preset = unit_preset

    g.user.eye_candy = request.form.get('eye_candy', False, type=bool)

    db.session.commit()

    return redirect(url_for('.index'))


@user_blueprint.route('/change_club', methods=['GET', 'POST'])
def change_club():
    if not g.user.is_writable(g.current_user):
        abort(403)

    change_form = ChangeClubForm(club=g.user.club_id)
    create_form = CreateClubForm()

    if request.endpoint.endswith('.change_club'):
        if change_form.validate_on_submit():
            return change_club_post(change_form)

    if request.endpoint.endswith('.create_club'):
        if create_form.validate_on_submit():
            return create_club_post(create_form)

    return render_template(
        'users/change_club.jinja',
        change_form=change_form, create_form=create_form)


@user_blueprint.route('/create_club', methods=['GET', 'POST'])
def create_club():
    return change_club()


def change_club_post(form):
    old_club_id = g.user.club_id
    new_club_id = form.club.data if form.club.data != 0 else None

    if old_club_id == new_club_id:
        return redirect(url_for('.index'))

    g.user.club_id = new_club_id

    create_club_join_event(new_club_id, g.user)

    # assign the user's new club to all of his flights that have
    # no club yet
    flights = Flight.query().join(IGCFile)
    flights = flights.filter(and_(Flight.club_id == None,
                                  or_(Flight.pilot_id == g.user.id,
                                      IGCFile.owner_id == g.user.id)))
    for flight in flights:
        flight.club_id = g.user.club_id

    db.session.commit()

    return redirect(url_for('.index'))


def create_club_post(form):
    club = Club(name=form.name.data)
    club.owner_id = g.current_user.id
    db.session.add(club)

    g.user.club = club

    db.session.commit()

    return redirect(url_for('.index'))


@user_blueprint.route('/follow')
@login_required
def follow():
    Follower.follow(g.current_user, g.user)
    create_follower_notification(g.user, g.current_user)
    db.session.commit()
    return redirect(url_for('.index'))


@user_blueprint.route('/unfollow')
@login_required
def unfollow():
    Follower.unfollow(g.current_user, g.user)
    db.session.commit()
    return redirect(url_for('.index'))


@user_blueprint.route('/tracking_register')
def tracking_register():
    if not g.user.is_writable(g.current_user):
        abort(403)

    g.user.generate_tracking_key()
    db.session.commit()

    return redirect(request.values.get('came_from', '/tracking/info'))


@user_blueprint.route('/recover_password')
def recover_password():
    if not g.current_user or not g.current_user.is_manager():
        abort(403)

    recover_user_password(g.user)
    flash('A password recovery email was sent to that user.')

    db.session.commit()

    return redirect(url_for('.index'))
