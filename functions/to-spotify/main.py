"""
Search track names on Spotify
"""

import os
import sys

# https://github.com/apex/apex/issues/639#issuecomment-455883587
file_path = os.path.dirname(__file__)
module_path = os.path.join(file_path, "env")
sys.path.append(module_path)

# https://stackoverflow.com/a/39293287/1515819
reload(sys)
sys.setdefaultencoding('utf8')

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

from sets import Set

import json
import decimal
import os
import time
import decimal

import spotipy
import spotipy.util as util
import spotipy.oauth2 as oauth2


# custom exceptions
class RATrackNotFoundException(Exception):
    pass


class SpotifyAPILimitReached(Exception):
    pass


# Helper class to convert a DynamoDB item to JSON.
class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            if o % 1 > 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)


class TrackName(str):
    def __new__(cls, content):
        content = ' '.join(content.split())    # sanitize new lines/tabs
        content = content.replace('\x00', '')  # sanitize null bytes
        return str.__new__(cls, content)

    def split_artist_and_track_name(self):
        return self.split(" - ", 1)

    @staticmethod
    def has_question_marks_only(str):
        allowed_chars = Set('?')
        return Set(str).issubset(allowed_chars)

    def has_missing_artist_or_name(self):
        try:
            artist, track_name = self.split_artist_and_track_name()
        except Exception as e:
            return True
        return TrackName.has_question_marks_only(artist) or \
            TrackName.has_question_marks_only(track_name)


class Memoize:
    def __init__(self, f):
        self.f = f
        self.memo = {}

    def __call__(self, *args):
        if args not in self.memo:
            self.memo[args] = self.f(*args)

        return self.memo[args]


# Helper class to convert a DynamoDB item to JSON.
class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            if o % 1 > 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)


LAMBDA_EXEC_TIME = 110
PLAYLIST_EXPECTED_MAX_LENGTH = 11000
MIN_YEAR = 2006

# DB
dynamodb = boto3.resource("dynamodb", region_name='eu-west-1')
cursors_table = dynamodb.Table('ra_cursors')
tracks_table = dynamodb.Table('any_tracks')
playlists_table = dynamodb.Table('ra_playlists')

# Spotify
SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
SPOTIPY_USER = os.getenv('SPOTIPY_USER')
SPOTIPY_REDIRECT_URI = 'http://localhost/'

scope = 'playlist-read-private playlist-modify-private playlist-modify-public'


@Memoize
def get_last_parsed_track(table):
    res = table.query(
        ScanIndexForward=False,
        KeyConditionExpression=Key('host').eq('ra'),
        Limit=1
    )
    if res['Count'] == 0:
        return 0
    return res['Items'][0]['id']


def restore_spotify_token():
    res = cursors_table.get_item(
        Key={
            'name': 'token'
        },
        AttributesToGet=[
            'value'
        ]
    )
    if 'Item' not in res:
        return 0

    token = res['Item']['value']
    with open("/tmp/.cache-"+SPOTIPY_USER, "w+") as f:
        f.write("%s" % json.dumps(token,
                                  ensure_ascii=False,
                                  cls=DecimalEncoder))

    print 'Restored token: %s' % token


def store_spotify_token(token_info):
    cursors_table.put_item(
        Item={
            'name': 'token',
            'value': token_info
        }
    )
    print "Stored token: %s" % token_info


def get_spotify():
    restore_spotify_token()

    sp_oauth = oauth2.SpotifyOAuth(
            SPOTIPY_CLIENT_ID,
            SPOTIPY_CLIENT_SECRET,
            SPOTIPY_REDIRECT_URI,
            scope=scope,
            cache_path='/tmp/.cache-'+SPOTIPY_USER
        )

    token_info = sp_oauth.get_cached_token()
    token_info = sp_oauth.refresh_access_token(token_info['refresh_token'])

    store_spotify_token(token_info)

    return spotipy.Spotify(auth=token_info['access_token'])


def find_on_spotify(sp, artist_and_track):
    query = 'track:"{0[1]}"+artist:"{0[0]}"'.format(artist_and_track)
    try:
        results = sp.search(query, limit=1, type='track')
        for _, t in enumerate(results['tracks']['items']):
            return t['uri']
    except Exception as e:
        raise e


def get_last_playlist_for_year(year):
    res = playlists_table.query(
        ScanIndexForward=False,
        KeyConditionExpression=Key('year').eq(year),
        Limit=1
    )
    if res['Count'] == 0:
        return
    return [res['Items'][0]['playlist_id'], res['Items'][0]['num']]


def create_playlist_for_year(sp, year, num=1):
    playlist_name = 'RA: %d' % year
    if num > 1:
        playlist_name += ' (%d)' % num
    res = sp.user_playlist_create(SPOTIPY_USER, playlist_name, public=True)
    playlists_table.put_item(
        Item={
            'year': year,
            'num': num,
            'playlist_id': res['id']
        }
    )
    return [res['id'], num]


def get_playlist(sp, year):
    return get_last_playlist_for_year(year) or \
           create_playlist_for_year(sp, year)


def playlist_seems_full(e, sp, playlist_id):
    if not (hasattr(e, 'http_status') and e.http_status in [403, 500]):
        return False
    # only query Spotify total as a last resort
    # https://github.com/spotify/web-api/issues/1179
    playlist = sp.user_playlist(SPOTIPY_USER, playlist_id, "tracks")
    total = playlist["tracks"]["total"]
    return total == PLAYLIST_EXPECTED_MAX_LENGTH


def add_track_to_spotify_playlist(sp, track_spotify_uri, year):
    try:
        playlist_id, playlist_num = get_playlist(sp, year)
        sp.user_playlist_add_tracks(SPOTIPY_USER,
                                    playlist_id,
                                    [track_spotify_uri])
    except Exception as e:
        if playlist_seems_full(e, sp, playlist_id):
            playlist_id, = create_playlist_for_year(sp,
                                                    year,
                                                    playlist_num+1)
            # retry same fonction to use API limit logic
            add_track_to_spotify_playlist(sp, track_spotify_uri, year)
        else:
            # Reached API limit?
            raise e
    return playlist_id


def get_cursor():
    res = cursors_table.get_item(
        Key={
            'name': 'rediscover'
        },
        AttributesToGet=[
            'position'
        ]
    )
    if 'Item' not in res:
        return 0

    return res['Item']['position']


def set_cursor(position):
    cursors_table.put_item(
        Item={
            'name': 'rediscover',
            'position': position
        }
    )


def get_track_from_dynamodb(track_id):
    res = tracks_table.get_item(
        Key={
            'host': 'ra',
            'id': track_id
        },
        AttributesToGet=[
            'track_uri',
            'name',
            'first_charted_year',
            'release_date_year',
            'playlist_id'
        ]
    )
    return res['Item']


def add_put_attribute(attributes, attribute_name, attribute_value):
    if attribute_value:
        attributes[attribute_name] = {
            'Value': attribute_value,
            'Action': 'PUT'
        }
    return attributes


def persist_track(cur, current_track, year,
                  track_uri=None,
                  playlist_id=None,
                  question_marks=None):
    attribute_updates = {}
    add_put_attribute(attribute_updates, 'track_uri', track_uri)
    add_put_attribute(attribute_updates, 'playlist_id', playlist_id)
    add_put_attribute(attribute_updates, 'question_marks', question_marks)

    if question_marks:
        print "%s - %s (%d) | ??????" % (cur, current_track['name'], year)
    else:
        print "%s - %s (%d) | %s in %s" % (cur, current_track['name'],
                                           year, track_uri, playlist_id)

    return tracks_table.update_item(
        Key={
            'host': 'ra',
            'id': cur
        },
        AttributeUpdates=attribute_updates
    )


def get_min_year(current_track):
    release_date_year = current_track['release_date_year']
    if 'first_charted_year' not in current_track:
        min_year = release_date_year
    else:
        min_year = min(release_date_year, current_track['first_charted_year'])
    return MIN_YEAR if min_year < MIN_YEAR else min_year


def handle_index(index, sp):
    try:
        current_track = get_track_from_dynamodb(index)
    except Exception:
        raise RATrackNotFoundException

    track = TrackName(current_track['name'])
    year = get_min_year(current_track)

    if track.has_missing_artist_or_name():
        persist_track(index, current_track, year, question_marks=True)
    elif not ('track_uri' in current_track or
              'question_marks' in current_track):
        track_uri = find_on_spotify(sp, track.split_artist_and_track_name())
        if track_uri:
            playlist_id = add_track_to_spotify_playlist(sp, track_uri, year)
            persist_track(index, current_track, year,
                          track_uri=track_uri,
                          playlist_id=playlist_id)


def handle(event, context):
    sp = get_spotify()
    now = begin_time = int(time.time())
    index = get_cursor()

    while now < begin_time + LAMBDA_EXEC_TIME:
        index += 1
        try:
            handle_index(index, sp)
        except RATrackNotFoundException as e:
            last_id = get_last_parsed_track(tracks_table)  # memoized
            if index >= last_id:
                index = 0
        except Exception as e:
            print e
            break
        finally:
            now = int(time.time())
        set_cursor(index)


if __name__ == "__main__":
    print handle({}, {})
