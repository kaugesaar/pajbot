#!/usr/bin/env python3

import argparse
import collections
import configparser
import datetime
import logging
import os
import subprocess
import sys
import urllib

from pajbot.apiwrappers import TwitchAPI
from pajbot.bot import Bot
from pajbot.managers import RedisManager
from pajbot.models.db import DBManager
from pajbot.models.duel import UserDuelStats
from pajbot.models.module import ModuleManager
from pajbot.models.pleblist import PleblistSong
from pajbot.models.sock import SocketClientManager
from pajbot.models.stream import Stream, StreamChunk, StreamChunkHighlight
from pajbot.models.time import TimeManager
from pajbot.models.user import User
from pajbot.models.webcontent import WebContent
from pajbot.streamhelper import StreamHelper
from pajbot.tbutil import find
from pajbot.tbutil import load_config, init_logging
from pajbot.web.models import errors
import pajbot.web.routes
import pajbot.web.common

from flask import Flask
from flask import Markup
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from flask.ext.scrypt import generate_random_salt
from flask_oauthlib.client import OAuth
from flask_oauthlib.client import OAuthException
from sqlalchemy import func, cast, Date
from sqlalchemy.orm import joinedload
import markdown

init_logging('pajbot')
log = logging.getLogger('pajbot')

app = Flask(__name__)
app._static_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

pajbot.web.routes.admin.init(app)
pajbot.web.routes.api.init(app)
pajbot.web.routes.base.init(app)

pajbot.web.common.filters.init(app)
pajbot.web.common.assets.init(app)

app.register_blueprint(pajbot.web.routes.clr.page)
app.register_blueprint(pajbot.web.routes.api.page)

config = configparser.ConfigParser()

parser = argparse.ArgumentParser(description='start the web app')
parser.add_argument('--config', default='config.ini')
parser.add_argument('--host', default='0.0.0.0')
parser.add_argument('--port', type=int, default=2325)
parser.add_argument('--debug', dest='debug', action='store_true')
parser.add_argument('--no-debug', dest='debug', action='store_false')
parser.set_defaults(debug=False)

args = parser.parse_args()

config = load_config(args.config)
config.read('webconfig.ini')

if 'web' not in config:
    log.error('Missing [web] section in config.ini')
    sys.exit(1)

if 'pleblist_password_salt' not in config['web']:
    salt = generate_random_salt()
    config.set('web', 'pleblist_password_salt', salt.decode('utf-8'))

if 'secret_key' not in config['web']:
    salt = generate_random_salt()
    config.set('web', 'secret_key', salt.decode('utf-8'))

if 'logo' not in config['web']:
    twitchapi = TwitchAPI()
    try:
        data = twitchapi.get(['users', config['main']['streamer']], base='https://api.twitch.tv/kraken/')
        log.info(data)
        if data:
            logo_raw = 'static/images/logo_{}.png'.format(config['main']['streamer'])
            logo_tn = 'static/images/logo_{}_tn.png'.format(config['main']['streamer'])
            with urllib.request.urlopen(data['logo']) as response, open(logo_raw, 'wb') as out_file:
                data = response.read()
                out_file.write(data)
                try:
                    from PIL import Image
                    im = Image.open(logo_raw)
                    im.thumbnail((64, 64), Image.ANTIALIAS)
                    im.save(logo_tn, 'png')
                except:
                    pass
            config.set('web', 'logo', 'set')
            log.info('set logo')
    except:
        pass

StreamHelper.init_web(config['main']['streamer'])

redis_options = {}
if 'redis' in config:
    redis_options = config._sections['redis']

RedisManager.init(**redis_options)

with open(args.config, 'w') as configfile:
    config.write(configfile)

app.secret_key = config['web']['secret_key']
oauth = OAuth(app)


if 'sock' in config and 'sock_file' in config['sock']:
    SocketClientManager.init(config['sock']['sock_file'])

twitch = oauth.remote_app(
        'twitch',
        consumer_key=config['webtwitchapi']['client_id'],
        consumer_secret=config['webtwitchapi']['client_secret'],
        request_token_params={'scope': 'user_read'},
        base_url='https://api.twitch.tv/kraken/',
        request_token_url=None,
        access_token_method='POST',
        access_token_url='https://api.twitch.tv/kraken/oauth2/token',
        authorize_url='https://api.twitch.tv/kraken/oauth2/authorize',
        )

DBManager.init(config['main']['db'])
TimeManager.init_timezone(config['main'].get('timezone', 'UTC'))

module_manager = ModuleManager(None).load()

with DBManager.create_session_scope() as db_session:
    custom_web_content = {}
    for web_content in db_session.query(WebContent).filter(WebContent.content is not None):
        custom_web_content[web_content.page] = web_content.content

errors.init(app)
pajbot.web.routes.api.config = config
pajbot.web.routes.clr.config = config

modules = config['web'].get('modules', '').split()

bot_commands_list = []

from flask import make_response
from functools import wraps, update_wrapper

def nocache(view):
    @wraps(view)
    def no_cache(*args, **kwargs):
        response = make_response(view(*args, **kwargs))
        response.headers['Last-Modified'] = datetime.datetime.now()
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '-1'
        return response

    return update_wrapper(no_cache, view)


def update_commands(signal_id):
    log.debug('Updating commands...')
    global bot_commands_list
    from pajbot.models.command import CommandManager
    bot_commands = CommandManager(
            socket_manager=None,
            module_manager=ModuleManager(None).load(),
            bot=None).load(load_examples=True)
    bot_commands_list = bot_commands.parse_for_web()

    bot_commands_list = sorted(bot_commands_list, key=lambda x: (x.id or -1, x.main_alias))
    del bot_commands


update_commands(26)
try:
    import uwsgi
    from uwsgidecorators import thread, timer
    uwsgi.register_signal(26, "worker", update_commands)
    uwsgi.add_timer(26, 60 * 10)

    @thread
    @timer(5)
    def get_highlight_thumbnails(no_clue_what_this_does):
        from pajbot.web.models.thumbnail import StreamThumbnailWriter
        with DBManager.create_session_scope() as db_session:
            highlights = db_session.query(StreamChunkHighlight).filter_by(thumbnail=None).all()
            if len(highlights) > 0:
                log.info('Writing {} thumbnails...'.format(len(highlights)))
                StreamThumbnailWriter(config['main']['streamer'], [h.id for h in highlights])
                log.info('Done!')
                for highlight in highlights:
                    highlight.thumbnail = True
except ImportError:
    log.exception('Import error, disregard if debugging.')
    pass

@app.route('/')
def index():
    custom_content = custom_web_content.get('home', '')
    try:
        custom_content = Markup(markdown.markdown(custom_content))
    except:
        log.exception('Unhandled exception in def index')

    redis = RedisManager.get()
    streamer = StreamHelper.get_streamer()

    keys = ('online', 'viewers', 'game')
    stream_data_keys = ['{streamer}:{key}'.format(streamer=streamer, key=key) for key in keys]
    stream_data_list = redis.hmget('stream_data', stream_data_keys)
    stream_data = {keys[x]: stream_data_list[x] for x in range(0, len(keys))}

    keys = StreamHelper.social_keys
    streamer_info_keys = ['{streamer}:{key}'.format(streamer=streamer, key=key) for key in keys.keys()]
    log.info(streamer_info_keys)
    streamer_info_list = redis.hmget('streamer_info', streamer_info_keys)
    streamer_info = collections.OrderedDict()
    for key in keys:
        value = streamer_info_list.pop(0)
        if value:
            streamer_info[key] = {
                    'value': keys[key]['format'].format(value),
                    'title': keys[key]['title'],
                    'format': keys[key]['format'],
                    }

    current_quest_key = '{streamer}:current_quest'.format(streamer=StreamHelper.get_streamer())
    current_quest_id = redis.get(current_quest_key)
    if current_quest_id is not None:
        current_quest = module_manager[current_quest_id]
        if current_quest:
            current_quest.load_data()
    else:
        current_quest = None

    return render_template('index.html',
            custom_content=custom_content,
            current_quest=current_quest,
            stream_data=stream_data,
            streamer_info=streamer_info)


@app.route('/user/<username>')
def user_profile(username):
    session = DBManager.create_session()
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None:
        return render_template('no_user.html'), 404

    rank = session.query(func.Count(User.id)).filter(User.points > user.points).one()
    rank = rank[0] + 1
    user.rank = rank

    user_duel_stats = session.query(UserDuelStats).filter_by(user_id=user.id).one_or_none()

    try:
        return render_template('user.html',
                user=user,
                user_duel_stats=user_duel_stats)
    finally:
        session.close()


@app.route('/points/')
def points():
    custom_content = custom_web_content.get('points', '')
    try:
        custom_content = Markup(markdown.markdown(custom_content))
    except:
        log.exception('Unhandled exception in def index')
    session = DBManager.create_session()
    top_30_users = []
    for user in session.query(User).order_by(User.points.desc())[:30]:
        top_30_users.append(user)
    session.close()
    return render_template('points.html',
            top_30_users=top_30_users,
            custom_content=custom_content)


@app.route('/debug')
def debug():
    return render_template('debug.html')


@app.route('/stats/')
def stats():
    top_5_commands = sorted(bot_commands_list, key=lambda c: c.data.num_uses if c.data is not None else -1, reverse=True)[:5]

    if 'linefarming' in modules:
        session = DBManager.create_session()
        top_5_line_farmers = session.query(User).order_by(User.num_lines.desc())[:5]
        session.close()
    else:
        top_5_line_farmers = []

    return render_template('stats.html',
            top_5_commands=top_5_commands,
            top_5_line_farmers=top_5_line_farmers)

@app.route('/stats/duels/')
def stats_duels():
    session = DBManager.create_session()

    data = {
            'top_5_winners': session.query(UserDuelStats).order_by(UserDuelStats.duels_won.desc())[:5],
            'top_5_points_won': session.query(UserDuelStats).order_by(UserDuelStats.profit.desc())[:5],
            'top_5_points_lost': session.query(UserDuelStats).order_by(UserDuelStats.profit.asc())[:5],
            'top_5_losers': session.query(UserDuelStats).order_by(UserDuelStats.duels_lost.desc())[:5],
            'top_5_winrate': session.query(UserDuelStats).filter(UserDuelStats.duels_won >= 5).order_by(UserDuelStats.winrate.desc())[:5],
            'bottom_5_winrate': session.query(UserDuelStats).filter(UserDuelStats.duels_lost >= 5).order_by(UserDuelStats.winrate.asc())[:5],
            }

    try:
        return render_template('stats_duels.html', **data)
    finally:
        session.close()

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/highlights/<date>/')
def highlight_list_date(date):
    # Make sure we were passed a valid date
    try:
        parsed_date = datetime.datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        # Invalid date
        return redirect('/highlights/', 303)
    session = DBManager.create_session()
    dates_with_highlights = []
    highlights = session.query(StreamChunkHighlight).options(joinedload('*')).filter(cast(StreamChunkHighlight.created_at, Date) == parsed_date).order_by(StreamChunkHighlight.created_at.desc()).all()
    for highlight in session.query(StreamChunkHighlight):
        dates_with_highlights.append(datetime.datetime(
            year=highlight.created_at.year,
            month=highlight.created_at.month,
            day=highlight.created_at.day))

    try:
        return render_template('highlights_date.html',
                highlights=highlights,
                date=parsed_date,
                dates_with_highlights=set(dates_with_highlights))
    finally:
        session.close()

@app.route('/highlights/<date>/<highlight_id>', defaults={'highlight_title': None})
@app.route('/highlights/<date>/<highlight_id>-<highlight_title>')
def highlight_id(date, highlight_id, highlight_title=None):
    with DBManager.create_session_scope() as db_session:
        highlight = db_session.query(StreamChunkHighlight).options(joinedload('*')).filter_by(id=highlight_id).first()
        if highlight is None:
            return render_template('highlight_404.html'), 404
        else:
            stream_chunk = highlight.stream_chunk
            stream = stream_chunk.stream
        return render_template('highlight.html',
                highlight=highlight,
                stream_chunk=stream_chunk,
                stream=stream)

@app.route('/highlights/')
def highlights():
    session = DBManager.create_session()
    dates_with_highlights = []
    highlights = session.query(StreamChunkHighlight).options(joinedload('*')).order_by(StreamChunkHighlight.created_at_with_offset.desc()).all()
    for highlight in highlights:
        dates_with_highlights.append(datetime.datetime(
            year=highlight.created_at.year,
            month=highlight.created_at.month,
            day=highlight.created_at.day))
    try:
        return render_template('highlights.html',
                highlights=highlights[:10],
                dates_with_highlights=set(dates_with_highlights))
    finally:
        session.close()


@app.route('/pleblist/')
def pleblist():
    return render_template('pleblist.html')

@app.route('/pleblist/host/')
def pleblist_host():
    return render_template('pleblist_host.html')

@app.route('/pleblist/history/')
def pleblist_history_redirect():
    with DBManager.create_session_scope() as session:
        current_stream = session.query(Stream).filter_by(ended=False).order_by(Stream.stream_start.desc()).first()
        if current_stream is not None:
            return redirect('/pleblist/history/{}/'.format(current_stream.id), 303)

        last_stream = session.query(Stream).filter_by(ended=True).order_by(Stream.stream_start.desc()).first()
        if last_stream is not None:
            return redirect('/pleblist/history/{}/'.format(last_stream.id), 303)

        return render_template('pleblist_history_no_stream.html'), 404

@app.route('/pleblist/history/<stream_id>/')
def pleblist_history_stream(stream_id):
    with DBManager.create_session_scope() as session:
        stream = session.query(Stream).filter_by(id=stream_id).one_or_none()
        if stream is None:
            return render_template('pleblist_history_404.html'), 404

        songs = session.query(PleblistSong).filter(PleblistSong.stream_id == stream.id).order_by(PleblistSong.id.asc(), PleblistSong.id.asc()).all()
        total_length_left = sum([song.skip_after or song.song_info.duration if song.date_played is None and song.song_info is not None else 0 for song in songs])

        first_unplayed_song = find(lambda song: song.date_played is None, songs)
        stream_chunks = session.query(StreamChunk).filter(StreamChunk.stream_id == stream.id).all()

        return render_template('pleblist_history.html',
                stream=stream,
                songs=songs,
                total_length_left=total_length_left,
                first_unplayed_song=first_unplayed_song,
                stream_chunks=stream_chunks)


@app.route('/notifications/')
def notifications():
    return render_template('notifications.html')

@app.route('/test/')
def test():
    redis = RedisManager.get()
    current_quest_key = '{streamer}:current_quest'.format(streamer=StreamHelper.get_streamer())
    current_quest_id = redis.get(current_quest_key)
    current_quest = module_manager[current_quest_id]
    current_quest.load_data()
    return render_template('test.html', current_quest=current_quest)

@app.route('/login')
def login():
    return twitch.authorize(callback=config['webtwitchapi']['redirect_uri'] if 'redirect_uri' in config['webtwitchapi'] else url_for('authorized', _external=True))

@app.route('/login/error')
def login_error():
    return render_template('login_error.html')

@app.route('/login/authorized')
def authorized():
    try:
        resp = twitch.authorized_response()
    except OAuthException:
        log.exception('An exception was caught while authorizing')
        return redirect(url_for('index'))

    print(resp)
    if resp is None:
        log.warn('Access denied: reason={}, error={}'.format(request.args['error'], request.args['error_description']))
        return redirect(url_for('index'))
    elif type(resp) is OAuthException:
        log.warn(resp.message)
        log.warn(resp.data)
        log.warn(resp.type)
        return redirect(url_for('login_error'))
    session['twitch_token'] = (resp['access_token'], )
    me = twitch.get('user')
    level = 100
    with DBManager.create_session_scope() as db_session:
        db_user = db_session.query(User).filter_by(username=me.data['name'].lower()).one_or_none()
        if db_user:
            level = db_user.level
    session['user'] = {
            'username': me.data['name'],
            'username_raw': me.data['display_name'],
            'level': level,
            }
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('twitch_token', None)
    session.pop('user', None)
    return redirect(url_for('index'))

@twitch.tokengetter
def get_twitch_oauth_token():
    return session.get('twitch_token')

def change_twitch_header(uri, headers, body):
    auth = headers.get('Authorization')
    if auth:
        auth = auth.replace('Bearer', 'OAuth')
        headers['Authorization'] = auth
    return uri, headers, body

twitch.pre_request = change_twitch_header

nav_bar_header = []
nav_bar_header.append(('/', 'home', 'Home'))
nav_bar_header.append(('/commands/', 'commands', 'Commands'))
if 'deck' in module_manager:
    nav_bar_header.append(('/decks/', 'decks', 'Decks'))
if config['main']['nickname'] not in ['scamazbot']:
    nav_bar_header.append(('/points/', 'points', 'Points'))
nav_bar_header.append(('/stats/', 'stats', 'Stats'))
nav_bar_header.append(('/highlights/', 'highlights', 'Highlights'))
if 'pleblist' in modules:
    nav_bar_header.append(('/pleblist/history/', 'pleblist', 'Pleblist'))

nav_bar_admin_header = []
nav_bar_admin_header.append(('/', 'home', 'Home'))
nav_bar_admin_header.append(('/admin/', 'admin_home', 'Admin Home'))
nav_bar_admin_header.append(([
    ('/admin/banphrases/', 'admin_banphrases', 'Banphrases'),
    ('/admin/links/blacklist/', 'admin_links_blacklist', 'Blacklisted links'),
    ('/admin/links/whitelist/', 'admin_links_whitelist', 'Whitelisted links'),
    ], None, 'Filters'))
nav_bar_admin_header.append(('/admin/commands/', 'admin_commands', 'Commands'))
nav_bar_admin_header.append(('/admin/timers/', 'admin_timers', 'Timers'))
nav_bar_admin_header.append(('/admin/moderators/', 'admin_moderators', 'Moderators'))
nav_bar_admin_header.append(('/admin/modules/', 'admin_modules', 'Modules'))
if 'predict' in module_manager:
    nav_bar_admin_header.append(('/admin/predictions/', 'admin_predictions', 'Predictions'))
nav_bar_admin_header.append(('/admin/streamer/', 'admin_streamer', 'Streamer Info'))
nav_bar_admin_header.append(('/admin/clr/', 'admin_clr', 'CLR'))

version = Bot.version
last_commit = ''
commit_number = 0
try:
    current_branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).decode('utf8').strip()
    latest_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode('utf8').strip()[:8]
    commit_number = subprocess.check_output(['git', 'rev-list', 'HEAD', '--count']).decode('utf8').strip()
    last_commit = subprocess.check_output(['git', 'log', '-1', '--format=%cd']).decode('utf8').strip()
    version = '{0} DEV ({1}, {2}, commit {3})'.format(version, current_branch, latest_commit, commit_number)
except:
    pass


default_variables = {
        'version': version,
        'last_commit': last_commit,
        'commit_number': commit_number,
        'bot': {
            'name': config['main']['nickname'],
            },
        'site': {
            'domain': config['web']['domain'],
            'deck_tab_images': config.getboolean('web', 'deck_tab_images'),
            'websocket': {
                'host': config['websocket'].get('host', config['web']['domain']),
                'port': config['websocket']['port'],
                'ssl': config.getboolean('websocket', 'ssl')
                }
            },
        'streamer': {
            'name': config['web']['streamer_name'],
            'full_name': config['main']['streamer']
            },
        'nav_bar_header': nav_bar_header,
        'nav_bar_admin_header': nav_bar_admin_header,
        'modules': modules,
        'request': request,
        'session': session,
        'google_analytics': config['web'].get('google_analytics', None),
        }

if 'streamtip' in config:
    default_variables['streamtip_data'] = {
            'client_id': config['streamtip']['client_id'],
            'redirect_uri': config['streamtip']['redirect_uri'],
            }
else:
    default_variables['streamtip_data'] = {
            'client_id': 'MISSING',
            'redirect_uri': 'MISSING',
            }

if 'twitchalerts' in config:
    default_variables['twitchalerts_data'] = {
            'client_id': config['twitchalerts']['client_id'],
            'redirect_uri': config['twitchalerts']['redirect_uri'],
            }
else:
    default_variables['twitchalerts_data'] = {
            'client_id': 'MISSING',
            'redirect_uri': 'MISSING',
            }

@app.context_processor
def current_time():
    current_time = {}
    current_time['current_time'] = datetime.datetime.now()
    return current_time

@app.context_processor
def inject_default_variables():
    return default_variables

if __name__ == '__main__':
    app.run(debug=args.debug, host=args.host, port=args.port)
