
import json
import time
import redis

from bottle import request, response, HTTPError
from datetime import datetime
from re import sub

from webrecorder.apiutils import CustomJSONEncoder
from webrecorder.basecontroller import BaseController
from webrecorder.schemas import (CollectionSchema, NewUserSchema, UserSchema,
                                 UserUpdateSchema)

from webrecorder.webreccork import ValidationException
from werkzeug.useragents import UserAgent


# ============================================================================
class UserController(BaseController):
    def __init__(self, app, jinja_env, manager, config):
        super(UserController, self).__init__(app, jinja_env, manager, config)
        self.default_user_desc = config['user_desc']

    def init_routes(self):

        @self.app.get(['/api/v1/dashboard', '/api/v1/dashboard/'])
        @self.manager.admin_view()
        def api_dashboard():
            cache_key = self.cache_template.format('dashboard')
            expiry = 5 * 60  # 5 min

            cache = self.manager.redis.get(cache_key)

            if cache:
                return json.loads(cache.decode('utf-8'))

            users = self.manager.get_users().items()
            results = []

            # add username and get collections
            for user, data in users:
                data['username'] = user
                data['collections'] = self.manager.get_collections(user,
                                                                   api=True)
                results.append(data)

            data = {
                # `results` is a list so will always read as `many`
                'users': UserSchema().load(results, many=True).data
            }

            self.manager.redis.setex(cache_key,
                                     expiry,
                                     json.dumps(data, cls=CustomJSONEncoder))

            return data


        @self.app.get(['/api/v1/users', '/api/v1/users/'])
        @self.manager.admin_view()
        def api_users():
            """Full admin API resource of all users.
               Containing user info and public collections

               - Provides basic (1 dimension) RESTful sorting
               - TODO: Pagination
            """
            sorting = request.query.getunicode('sort', None)
            sort_key = sub(r'^-{1}?', '', sorting) if sorting is not None else None
            reverse = sorting.startswith('-') if sorting is not None else False

            def dt(d):
                return datetime.strptime(d, '%Y-%m-%d %H:%M:%S.%f')

            # sortable fields, with optional key unpacking functions
            filters = {
                'created': {'key': lambda obj: dt(obj[1]['creation_date'])},
                'email': {'key': lambda obj: obj[1]['email_addr']},
                'last_login': {'key': lambda obj: dt(obj[1]['last_login'])},
                'name': {'key': lambda obj: json.loads(obj[1]['desc'] or '{}')['name']},
                'username': {},
            }

            if sorting is not None and sort_key not in filters:
                raise HTTPError(400, 'Bad Request')

            sort_by = filters[sort_key] if sorting is not None else {}
            users = sorted(self.manager.get_users().items(),
                           **sort_by,
                           reverse=reverse)

            results = []

            # add username and get collections
            for user, data in users:
                data['username'] = user
                # add space usage
                total = self.manager.get_size_allotment(user)
                used = self.manager.get_size_usage(user)
                data['space_utilization'] = {
                    'total': total,
                    'used': used,
                    'available': total - used,
                }
                results.append(data)

            return {
                # `results` is a list so will always read as `many`
                'users': UserSchema().load(results, many=True).data
            }

        @self.app.get('/api/v1/anon_user')
        def get_anon_user():
            return {'anon_user': self.manager.get_anon_user(True)}

        @self.app.post('/api/v1/users/<user>/desc')
        def update_desc(user):
            """legacy, eventually move to the patch endpoint"""
            desc = request.body.read().decode('utf-8')

            self.manager.set_user_desc(user, desc)
            return {}

        @self.app.post(['/api/v1/users', '/api/v1/users/'])
        @self.manager.admin_view()
        def api_create_user():
            """API enpoint to create a user with schema validation"""
            users = self.manager.get_users()
            emails = [u[1]['email_addr'] for u in users.items()]
            data = request.json
            err = NewUserSchema().validate(data)

            if 'username' in data and data['username'] in users:
                if not err:
                    return {'errors': 'Username already exists'}
                else:
                    err.update({'username': 'Username already exists'})

            if 'email' in data and data['email'] in emails:
                if not err:
                    return {'errors': 'Email already exists'}
                else:
                    err.update({'email': 'Email already exists'})

            # validate
            if len(err):
                return {'errors': err}

            # create user
            self.manager.cork._store.users[data['username']] = {
                'role': data['role'],
                'hash': self.manager.cork._hash(data['username'],
                                                data['password']).decode('ascii'),
                'email_addr': data['email'],
                'desc': '{{"name":"{name}"}}'.format(name=data.get('name', '')),
                'creation_date': str(datetime.utcnow()),
                'last_login': str(datetime.utcnow()),
            }
            self.manager.cork._store.save_users()

            # add user account defaults
            key = self.manager.user_key.format(user=data['username'])
            now = int(time.time())

            max_size, max_coll = self.manager.redis.hmget('h:defaults',
                                                          ['max_size', 'max_coll'])
            if not max_size:
                max_size = self.manager.default_max_size

            if not max_coll:
                max_coll = self.manager.default_max_coll

            with redis.utils.pipeline(self.manager.redis) as pi:
                pi.hset(key, 'max_size', max_size)
                pi.hset(key, 'max_coll', max_coll)
                pi.hset(key, 'created_at', now)
                pi.hset(key, 'name', data.get('name', ''))
                pi.hsetnx(key, 'size', '0')

            # create initial collection
            self.manager.create_collection(
                data['username'],
                coll=self.manager.default_coll['id'],
                coll_title=self.manager.default_coll['title'],
                desc=self.manager.default_coll['desc'].format(data['username']),
                public=False,
                synthetic=True
            )

            # Check for mailing list management
            if self.manager.mailing_list:
                self.manager.add_to_mailing_list(
                    data['username'],
                    data['email'],
                    data.get('name', ''),
                )

        @self.app.get(['/api/v1/users/<username>', '/api/v1/users/<username>/'])
        @self.manager.admin_view()
        def api_get_user(username):
            """API enpoint to return user info"""
            users = self.manager.get_users()

            if username not in users:
                self._raise_error(404, 'No such user')

            user = users[username]

            # assemble space usage
            total = self.manager.get_size_allotment(username)
            used = self.manager.get_size_usage(username)
            user['space_utilization'] = {
                'total': total,
                'used': used,
                'available': total - used,
            }

            user_data, err = UserSchema(exclude=('username',)).load(user)
            colls = self.manager.get_collections(username,
                                                 include_recs=True,
                                                 api=True)

            for coll in colls:
                for rec in coll['recordings']:
                    rec['pages'] = self.manager.list_pages(username,
                                                           coll['id'],
                                                           rec['id'])

            # colls is a list so will always be `many` even if one collection
            collections, err = CollectionSchema().load(colls, many=True)
            user_data['collections'] = collections

            return {'user': user_data}

        @self.app.put(['/api/v1/users/<username>', '/api/v1/users/<username>/'])
        @self.manager.auth_view()
        def api_update_user(username):
            """API enpoint to update user info

               See `UserUpdateSchema` for available fields.

               ** bottle 0.12.9 doesn't support `PATCH` methods.. update to
                  patch once availabile.
            """
            users = self.manager.get_users()
            if username not in users:
                self._raise_error(404, 'No such user')

            # if not admin, check ownership
            if not self.manager.is_anon(username) and not self.manager.is_superuser():
                self.manager.assert_user_is_owner(username)

            user = users[username]
            try:
                json_data = json.loads(request.forms.json)
            except Exception as e:
                print(e)
                return {'errors': 'bad json data'}

            if len(json_data.keys()) == 0:
                return {'errors': 'empty payload'}

            data, err = UserUpdateSchema(only=json_data.keys()).load(json_data)

            if len(err):
                return {'errors': err}

            if 'name' in data:
                user['desc'] = '{{"name":"{name}"}}'.format(name=data.get('name', ''))

            #
            # restricted resources
            #
            if 'max_size' in data and self.manager.is_superuser():
                key = self.manager.user_key.format(user=username)
                max_size = data.get('max_size', self.manager.default_max_size)
                max_size = int(max_size) if type(max_size) is not int else max_size

                with redis.utils.pipeline(self.manager.redis) as pi:
                    pi.hset(key, 'max_size', max_size)

            if 'role' in data and self.manager.is_superuser():
                # set new role or default to base role
                user['role'] = data.get('role', 'archivist')

            #
            # return updated user data
            #
            total = self.manager.get_size_allotment(username)
            used = self.manager.get_size_usage(username)
            user['space_utilization'] = {
                'total': total,
                'used': used,
                'available': total - used,
            }

            user_data, err = UserSchema(exclude=('username',)).load(user)
            colls = self.manager.get_collections(username,
                                                 include_recs=True,
                                                 api=True)

            for coll in colls:
                for rec in coll['recordings']:
                    rec['pages'] = self.manager.list_pages(username,
                                                           coll['id'],
                                                           rec['id'])

            # colls is a list so will always be `many` even if one collection
            collections, err = CollectionSchema().load(colls, many=True)
            user_data['collections'] = collections

            return {'user': user_data}

        @self.app.delete(['/api/v1/users/<user>', '/api/v1/users/<user>/'])
        @self.manager.admin_view()
        def api_delete_user(user):
            """API enpoint to delete a user"""
            if user not in self.manager.get_users():
                self._raise_error(404, 'No such user')

            self.manager.delete_user(user)

        @self.app.get(['/<user>', '/<user>/'])
        @self.jinja2_view('user.html')
        def user_info(user):
            self.redir_host()

            if self.manager.is_anon(user):
                self.redirect('/' + user + '/temp')

            self.manager.assert_user_exists(user)

            result = {
                'user': user,
                'user_info': self.manager.get_user_info(user),
                'collections': self.manager.get_collections(user),
            }

            if not result['user_info'].get('desc'):
                result['user_info']['desc'] = self.default_user_desc.format(user)

            return result

        # User Account Settings
        @self.app.get('/<user>/_settings')
        @self.jinja2_view('account.html')
        def account_settings(user):
            self.manager.assert_user_is_owner(user)

            return {'user': user,
                    'user_info': self.manager.get_user_info(user),
                    'num_coll': self.manager.num_collections(user),
                   }

        # Delete User Account
        @self.app.post('/<user>/$delete')
        def delete_user(user):
            if self.manager.delete_user(user):
                self.flash_message('The user {0} has been permanently deleted!'.format(user), 'success')

                redir_to = '/'
                request.environ['webrec.delete_all_cookies'] = 'all'
                self.manager.cork.logout(success_redirect=redir_to, fail_redirect=redir_to)
            else:
                self.flash_message('There was an error deleting {0}'.format(coll))
                self.redirect(self.get_path(user))

        # Expiry Message
        @self.app.route('/_expire')
        def expire():
            self.flash_message('Sorry, the anonymous collection has expired due to inactivity')
            self.redirect('/')

        @self.app.post('/_reportissues')
        def report_issues():
            useragent = request.headers.get('User-Agent')

            @self.jinja2_view('email_error.html')
            def error_email(params):
                ua = UserAgent(params.get('ua'))
                if ua.browser:
                    browser = '{0} {1} {2} {3}'
                    lang = ua.language or ''
                    browser = browser.format(ua.platform, ua.browser,
                                             ua.version, lang)

                    params['browser'] = browser
                else:
                    params['browser'] = ua.string

                params['time'] = params['time'][:19]
                return params

            self.manager.report_issues(request.POST, useragent, error_email)
            return {}

        # Skip POST request recording
        @self.app.get('/_skipreq')
        def skip_req():
            url = request.query.getunicode('url')
            user = self.manager.get_curr_user()
            if not user:
                user = self.manager.get_anon_user()

            self.manager.skip_post_req(user, url)
            return {}





