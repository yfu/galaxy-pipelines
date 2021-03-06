"""
Galaxy web application framework
"""

import inspect
import os
import pkg_resources
import random
import socket
import string
import time
from Cookie import CookieError


pkg_resources.require( "Cheetah" )
from Cheetah.Template import Template
import base
from functools import wraps
from galaxy import util
from galaxy.exceptions import MessageException
from galaxy.util.json import to_json_string, from_json_string
from galaxy.util.backports.importlib import import_module
from galaxy.util.sanitize_html import sanitize_html

pkg_resources.require( "simplejson" )
import simplejson

import helpers

from galaxy.util import asbool

import paste.httpexceptions

pkg_resources.require( "Mako" )
import mako.template
import mako.lookup
import mako.runtime

pkg_resources.require( "Babel" )
from babel.support import Translations
from babel import Locale, UnknownLocaleError

pkg_resources.require( "SQLAlchemy >= 0.4" )
from sqlalchemy import and_
from sqlalchemy.orm.exc import NoResultFound

pkg_resources.require( "pexpect" )
pkg_resources.require( "amqplib" )

import logging
log = logging.getLogger( __name__ )

url_for = base.routes.url_for

UCSC_SERVERS = (
    'hgw1.cse.ucsc.edu',
    'hgw2.cse.ucsc.edu',
    'hgw3.cse.ucsc.edu',
    'hgw4.cse.ucsc.edu',
    'hgw5.cse.ucsc.edu',
    'hgw6.cse.ucsc.edu',
    'hgw7.cse.ucsc.edu',
    'hgw8.cse.ucsc.edu',
)

def expose( func ):
    """
    Decorator: mark a function as 'exposed' and thus web accessible
    """
    func.exposed = True
    return func

def json( func ):
    @wraps(func)
    def decorator( self, trans, *args, **kwargs ):
        trans.response.set_content_type( "text/javascript" )
        return simplejson.dumps( func( self, trans, *args, **kwargs ) )
    if not hasattr(func, '_orig'):
        decorator._orig = func
    decorator.exposed = True
    return decorator

def json_pretty( func ):
    @wraps(func)
    def decorator( self, trans, *args, **kwargs ):
        trans.response.set_content_type( "text/javascript" )
        return simplejson.dumps( func( self, trans, *args, **kwargs ), indent=4, sort_keys=True )
    if not hasattr(func, '_orig'):
        decorator._orig = func
    decorator.exposed = True
    return decorator

def require_login( verb="perform this action", use_panels=False, webapp='galaxy' ):
    def argcatcher( func ):
        @wraps(func)
        def decorator( self, trans, *args, **kwargs ):
            if trans.get_user():
                return func( self, trans, *args, **kwargs )
            else:
                return trans.show_error_message(
                    'You must be <a target="galaxy_main" href="%s">logged in</a> to %s.'
                    % ( url_for( controller='user', action='login', webapp=webapp ), verb ), use_panels=use_panels )
        return decorator
    return argcatcher

def expose_api_raw( func ):
    """
    Expose this function via the API but don't dump the results
    to JSON.
    """
    return expose_api( func, to_json=False )

def expose_api_anonymous( func, to_json=True ):
    """
    Expose this function via the API but don't require a set user.
    """
    return expose_api( func, to_json=to_json, user_required=False )

def expose_api( func, to_json=True, user_required=True ):
    """
    Expose this function via the API.
    """
    @wraps(func)
    def decorator( self, trans, *args, **kwargs ):
        def error( environ, start_response ):
            start_response( error_status, [('Content-type', 'text/plain')] )
            return error_message
        error_status = '403 Forbidden'
        if trans.error_message:
            return trans.error_message
        if user_required and trans.user is None:
            error_message = "API Authentication Required for this request"
            return error
        if trans.request.body:
            def extract_payload_from_request(trans, func, kwargs):
                content_type = trans.request.headers['content-type']
                if content_type.startswith('application/x-www-form-urlencoded') or content_type.startswith('multipart/form-data'):
                    # If the content type is a standard type such as multipart/form-data, the wsgi framework parses the request body
                    # and loads all field values into kwargs. However, kwargs also contains formal method parameters etc. which
                    # are not a part of the request body. This is a problem because it's not possible to differentiate between values
                    # which are a part of the request body, and therefore should be a part of the payload, and values which should not be
                    # in the payload. Therefore, the decorated method's formal arguments are discovered through reflection and removed from
                    # the payload dictionary. This helps to prevent duplicate argument conflicts in downstream methods.
                    payload = kwargs.copy()
                    named_args, _, _, _ = inspect.getargspec(func)
                    for arg in named_args:
                        payload.pop(arg, None)
                    for k, v in payload.iteritems():
                        if isinstance(v, (str, unicode)):
                            try:
                                payload[k] = simplejson.loads(v)
                            except:
                                # may not actually be json, just continue
                                pass
                    payload = util.recursively_stringify_dictionary_keys( payload )
                else:
                    # Assume application/json content type and parse request body manually, since wsgi won't do it. However, the order of this check
                    # should ideally be in reverse, with the if clause being a check for application/json and the else clause assuming a standard encoding
                    # such as multipart/form-data. Leaving it as is for backward compatibility, just in case.
                    payload = util.recursively_stringify_dictionary_keys( simplejson.loads( trans.request.body ) )
                return payload
            try:
                kwargs['payload'] = extract_payload_from_request(trans, func, kwargs)
            except ValueError:
                error_status = '400 Bad Request'
                error_message = 'Your request did not appear to be valid JSON, please consult the API documentation'
                return error
        trans.response.set_content_type( "application/json" )
        # send 'do not cache' headers to handle IE's caching of ajax get responses
        trans.response.headers[ 'Cache-Control' ] = "max-age=0,no-cache,no-store"
        # Perform api_run_as processing, possibly changing identity
        if 'payload' in kwargs and 'run_as' in kwargs['payload']:
            if not trans.user_can_do_run_as():
                error_message = 'User does not have permissions to run jobs as another user'
                return error
            try:
                decoded_user_id = trans.security.decode_id( kwargs['payload']['run_as'] )
            except TypeError:
                trans.response.status = 400
                return "Malformed user id ( %s ) specified, unable to decode." % str( kwargs['payload']['run_as'] )
            try:
                user = trans.sa_session.query( trans.app.model.User ).get( decoded_user_id )
                trans.api_inherit_admin = trans.user_is_admin()
                trans.set_user(user)
            except:
                trans.response.status = 400
                return "That user does not exist."
        try:
            rval = func( self, trans, *args, **kwargs)
            if to_json and trans.debug:
                rval = simplejson.dumps( rval, indent=4, sort_keys=True )
            elif to_json:
                rval = simplejson.dumps( rval )
            return rval
        except paste.httpexceptions.HTTPException:
            raise # handled
        except:
            log.exception( 'Uncaught exception in exposed API method:' )
            raise paste.httpexceptions.HTTPServerError()
    if not hasattr(func, '_orig'):
        decorator._orig = func
    decorator.exposed = True
    return decorator

def require_admin( func ):
    @wraps(func)
    def decorator( self, trans, *args, **kwargs ):
        if not trans.user_is_admin():
            msg = "You must be an administrator to access this feature."
            admin_users = trans.app.config.get( "admin_users", "" ).split( "," )
            user = trans.get_user()
            if not admin_users:
                msg = "You must be logged in as an administrator to access this feature, but no administrators are set in the Galaxy configuration."
            elif not user:
                msg = "You must be logged in as an administrator to access this feature."
            trans.response.status = 403
            if trans.response.get_content_type() == 'application/json':
                return msg
            else:
                return trans.show_error_message( msg )
        return func( self, trans, *args, **kwargs )
    return decorator

NOT_SET = object()

def error( message ):
    raise MessageException( message, type='error' )

def form( *args, **kwargs ):
    return FormBuilder( *args, **kwargs )


class WebApplication( base.WebApplication ):

    def __init__( self, galaxy_app, session_cookie='galaxysession', name=None ):
        self.name = name
        base.WebApplication.__init__( self )
        self.set_transaction_factory( lambda e: self.transaction_chooser( e, galaxy_app, session_cookie ) )
        # Mako support
        self.mako_template_lookup = self.create_mako_template_lookup( galaxy_app, name )
        # Security helper
        self.security = galaxy_app.security

    def create_mako_template_lookup( self, galaxy_app, name ):
        paths = []
        # First look in webapp specific directory
        if name is not None:
            paths.append( os.path.join( galaxy_app.config.template_path, 'webapps', name ) )
        # Then look in root directory
        paths.append( galaxy_app.config.template_path )
        # Create TemplateLookup with a small cache
        return mako.lookup.TemplateLookup(
            directories = paths,
            module_directory = galaxy_app.config.template_cache,
            collection_size = 500,
            output_encoding = 'utf-8' )

    def handle_controller_exception( self, e, trans, **kwargs ):
        if isinstance( e, MessageException ):
            #In the case of a controller exception, sanitize to make sure unsafe html input isn't reflected back to the user
            return trans.show_message( sanitize_html(e.err_msg), e.type )

    def make_body_iterable( self, trans, body ):
        if isinstance( body, FormBuilder ):
            body = trans.show_form( body )
        return base.WebApplication.make_body_iterable( self, trans, body )

    def transaction_chooser( self, environ, galaxy_app, session_cookie ):
        return GalaxyWebTransaction( environ, galaxy_app, self, session_cookie )

    def add_ui_controllers( self, package_name, app ):
        """
        Search for UI controllers in `package_name` and add 
        them to the webapp.
        """
        from galaxy.web.base.controller import BaseUIController
        from galaxy.web.base.controller import ControllerUnavailable
        package = import_module( package_name )
        controller_dir = package.__path__[0]
        for fname in os.listdir( controller_dir ):
            if not( fname.startswith( "_" ) ) and fname.endswith( ".py" ):
                name = fname[:-3]
                module_name = package_name + "." + name
                try:
                    module = import_module( module_name )
                except ControllerUnavailable, exc:
                    log.debug("%s could not be loaded: %s" % (module_name, str(exc)))
                    continue
                # Look for a controller inside the modules
                for key in dir( module ):
                    T = getattr( module, key )
                    if inspect.isclass( T ) and T is not BaseUIController and issubclass( T, BaseUIController ):
                        self.add_ui_controller( name, T( app ) )

    def add_api_controllers( self, package_name, app ):
        """
        Search for UI controllers in `package_name` and add 
        them to the webapp.
        """
        from galaxy.web.base.controller import BaseAPIController
        from galaxy.web.base.controller import ControllerUnavailable
        package = import_module( package_name )
        controller_dir = package.__path__[0]
        for fname in os.listdir( controller_dir ):
            if not( fname.startswith( "_" ) ) and fname.endswith( ".py" ):
                name = fname[:-3]
                module_name = package_name + "." + name
                try:
                    module = import_module( module_name )
                except ControllerUnavailable, exc:
                    log.debug("%s could not be loaded: %s" % (module_name, str(exc)))
                    continue
                for key in dir( module ):
                    T = getattr( module, key )
                    # Exclude classes such as BaseAPIController and BaseTagItemsController
                    if inspect.isclass( T ) and not key.startswith("Base") and issubclass( T, BaseAPIController ):
                        # By default use module_name, but allow controller to override name
                        controller_name = getattr( T, "controller_name", name )
                        self.add_api_controller( controller_name, T( app ) )


class GalaxyWebTransaction( base.DefaultWebTransaction ):
    """
    Encapsulates web transaction specific state for the Galaxy application
    (specifically the user's "cookie" session and history)
    """
    def __init__( self, environ, app, webapp, session_cookie = None):
        self.app = app
        self.webapp = webapp
        self.security = webapp.security
        base.DefaultWebTransaction.__init__( self, environ )
        self.setup_i18n()
        self.sa_session.expunge_all()
        self.debug = asbool( self.app.config.get( 'debug', False ) )
        # Flag indicating whether we are in workflow building mode (means
        # that the current history should not be used for parameter values
        # and such).
        self.workflow_building_mode = False
        # Flag indicating whether this is an API call and the API key user is an administrator
        self.api_inherit_admin = False
        self.__user = None
        self.galaxy_session = None
        self.error_message = None
        if self.environ.get('is_api_request', False):
            # With API requests, if there's a key, use it and associate the
            # user with the transaction.
            # If not, check for an active session but do not create one.
            # If an error message is set here, it's sent back using
            # trans.show_error in the response -- in expose_api.
            self.error_message = self._authenticate_api( session_cookie )
        else:
            #This is a web request, get or create session.
            self._ensure_valid_session( session_cookie )
        if self.galaxy_session:
            # When we've authenticated by session, we have to check the
            # following.
            # Prevent deleted users from accessing Galaxy
            if self.app.config.use_remote_user and self.galaxy_session.user.deleted:
                self.response.send_redirect( url_for( '/static/user_disabled.html' ) )
            if self.app.config.require_login:
                self._ensure_logged_in_user( environ, session_cookie )

    def setup_i18n( self ):
        locales = []
        if 'HTTP_ACCEPT_LANGUAGE' in self.environ:
            # locales looks something like: ['en', 'en-us;q=0.7', 'ja;q=0.3']
            client_locales = self.environ['HTTP_ACCEPT_LANGUAGE'].split( ',' )
            for locale in client_locales:
                try:
                    locales.append( Locale.parse( locale.split( ';' )[0].strip(), sep='-' ).language )
                except Exception, e:
                    log.debug( "Error parsing locale '%s'. %s: %s", locale, type( e ), e )
        if not locales:
            # Default to English
            locales = 'en'
        t = Translations.load( dirname='locale', locales=locales, domain='ginga' )
        self.template_context.update ( dict( _=t.ugettext, n_=t.ugettext, N_=t.ungettext ) )

    @property
    def sa_session( self ):
        """
        Returns a SQLAlchemy session -- currently just gets the current
        session from the threadlocal session context, but this is provided
        to allow migration toward a more SQLAlchemy 0.4 style of use.
        """
        return self.app.model.context.current


    def get_user( self ):
        """Return the current user if logged in or None."""
        if self.galaxy_session:
            return self.galaxy_session.user
        else:
            return self.__user

    def set_user( self, user ):
        """Set the current user."""
        if self.galaxy_session:
            self.galaxy_session.user = user
            self.sa_session.add( self.galaxy_session )
            self.sa_session.flush()
        self.__user = user

    user = property( get_user, set_user )

    def log_action( self, user=None, action=None, context=None, params=None):
        """
        Application-level logging of user actions.
        """
        if self.app.config.log_actions:
            action = self.app.model.UserAction(action=action, context=context, params=unicode( to_json_string( params ) ) )
            try:
                if user:
                    action.user = user
                else:
                    action.user = self.user
            except:
                action.user = None
            try:
                action.session_id = self.galaxy_session.id
            except:
                action.session_id = None
            self.sa_session.add( action )
            self.sa_session.flush()

    def log_event( self, message, tool_id=None, **kwargs ):
        """
        Application level logging. Still needs fleshing out (log levels and such)
        Logging events is a config setting - if False, do not log.
        """
        if self.app.config.log_events:
            event = self.app.model.Event()
            event.tool_id = tool_id
            try:
                event.message = message % kwargs
            except:
                event.message = message
            try:
                event.history = self.get_history()
            except:
                event.history = None
            try:
                event.history_id = self.history.id
            except:
                event.history_id = None
            try:
                event.user = self.user
            except:
                event.user = None
            try:
                event.session_id = self.galaxy_session.id
            except:
                event.session_id = None
            self.sa_session.add( event )
            self.sa_session.flush()

    def get_cookie( self, name='galaxysession' ):
        """Convenience method for getting a session cookie"""
        try:
            # If we've changed the cookie during the request return the new value
            if name in self.response.cookies:
                return self.response.cookies[name].value
            else:
                return self.request.cookies[name].value
        except:
            return None

    def set_cookie( self, value, name='galaxysession', path='/', age=90, version='1' ):
        """Convenience method for setting a session cookie"""
        # The galaxysession cookie value must be a high entropy 128 bit random number encrypted
        # using a server secret key.  Any other value is invalid and could pose security issues.
        self.response.cookies[name] = value
        self.response.cookies[name]['path'] = path
        self.response.cookies[name]['max-age'] = 3600 * 24 * age # 90 days
        tstamp = time.localtime ( time.time() + 3600 * 24 * age )
        self.response.cookies[name]['expires'] = time.strftime( '%a, %d-%b-%Y %H:%M:%S GMT', tstamp )
        self.response.cookies[name]['version'] = version
        try:
            self.response.cookies[name]['httponly'] = True
        except CookieError, e:
            log.warning( "Error setting httponly attribute in cookie '%s': %s" % ( name, e ) )

    def _authenticate_api( self, session_cookie ):
        """
        Authenticate for the API via key or session (if available).
        """
        api_key = self.request.params.get('key', None)
        secure_id = self.get_cookie( name=session_cookie )
        if self.environ.get('is_api_request', False) and api_key:
            # Sessionless API transaction, we just need to associate a user.
            try:
                provided_key = self.sa_session.query( self.app.model.APIKeys ).filter( self.app.model.APIKeys.table.c.key == api_key ).one()
            except NoResultFound:
                return 'Provided API key is not valid.'
            if provided_key.user.deleted:
                return 'User account is deactivated, please contact an administrator.'
            newest_key = provided_key.user.api_keys[0]
            if newest_key.key != provided_key.key:
                return 'Provided API key has expired.'
            self.set_user( provided_key.user )
        elif secure_id:
            # API authentication via active session
            # Associate user using existing session
            self._ensure_valid_session( session_cookie )
        else:
            # Anonymous API interaction -- anything but @expose_api_anonymous will fail past here.
            self.user = None
            self.galaxy_session = None

    def _ensure_valid_session( self, session_cookie, create=True):
        """
        Ensure that a valid Galaxy session exists and is available as
        trans.session (part of initialization)

        Support for universe_session and universe_user cookies has been
        removed as of 31 Oct 2008.
        """
        # Try to load an existing session
        secure_id = self.get_cookie( name=session_cookie )
        galaxy_session = None
        prev_galaxy_session = None
        user_for_new_session = None
        invalidate_existing_session = False
        # Track whether the session has changed so we can avoid calling flush
        # in the most common case (session exists and is valid).
        galaxy_session_requires_flush = False
        if secure_id:
            # Decode the cookie value to get the session_key
            session_key = self.security.decode_guid( secure_id )
            try:
                # Make sure we have a valid UTF-8 string
                session_key = session_key.encode( 'utf8' )
            except UnicodeDecodeError:
                # We'll end up creating a new galaxy_session
                session_key = None
            if session_key:
                # Retrieve the galaxy_session id via the unique session_key
                galaxy_session = self.sa_session.query( self.app.model.GalaxySession ) \
                                                .filter( and_( self.app.model.GalaxySession.table.c.session_key==session_key,
                                                               self.app.model.GalaxySession.table.c.is_valid==True ) ) \
                                                .first()
        # If remote user is in use it can invalidate the session and in some
        # cases won't have a cookie set above, so we need to to check some
        # things now.
        if self.app.config.use_remote_user:
            #If this is an api request, and they've passed a key, we let this go.
            assert "HTTP_REMOTE_USER" in self.environ, \
                "use_remote_user is set but no HTTP_REMOTE_USER variable"
            remote_user_email = self.environ[ 'HTTP_REMOTE_USER' ]
            if galaxy_session:
                # An existing session, make sure correct association exists
                if galaxy_session.user is None:
                    # No user, associate
                    galaxy_session.user = self.get_or_create_remote_user( remote_user_email )
                    galaxy_session_requires_flush = True
                elif galaxy_session.user.email != remote_user_email:
                    # Session exists but is not associated with the correct remote user
                    invalidate_existing_session = True
                    user_for_new_session = self.get_or_create_remote_user( remote_user_email )
                    log.warning( "User logged in as '%s' externally, but has a cookie as '%s' invalidating session",
                                 remote_user_email, galaxy_session.user.email )
            else:
                # No session exists, get/create user for new session
                user_for_new_session = self.get_or_create_remote_user( remote_user_email )
        else:
            if galaxy_session is not None and galaxy_session.user and galaxy_session.user.external:
                # Remote user support is not enabled, but there is an existing
                # session with an external user, invalidate
                invalidate_existing_session = True
                log.warning( "User '%s' is an external user with an existing session, invalidating session since external auth is disabled",
                             galaxy_session.user.email )
            elif galaxy_session is not None and galaxy_session.user is not None and galaxy_session.user.deleted:
                invalidate_existing_session = True
                log.warning( "User '%s' is marked deleted, invalidating session" % galaxy_session.user.email )
        # Do we need to invalidate the session for some reason?
        if invalidate_existing_session:
            prev_galaxy_session = galaxy_session
            prev_galaxy_session.is_valid = False
            galaxy_session = None
        # No relevant cookies, or couldn't find, or invalid, so create a new session
        if galaxy_session is None:
            galaxy_session = self.__create_new_session( prev_galaxy_session, user_for_new_session )
            galaxy_session_requires_flush = True
            self.galaxy_session = galaxy_session
            self.__update_session_cookie( name=session_cookie )
        else:
            self.galaxy_session = galaxy_session
        # Do we need to flush the session?
        if galaxy_session_requires_flush:
            self.sa_session.add( galaxy_session )
            # FIXME: If prev_session is a proper relation this would not
            #        be needed.
            if prev_galaxy_session:
                self.sa_session.add( prev_galaxy_session )
            self.sa_session.flush()
        # If the old session was invalid, get a new history with our new session
        if invalidate_existing_session:
            self.new_history()

    def _ensure_logged_in_user( self, environ, session_cookie ):
        # The value of session_cookie can be one of
        # 'galaxysession' or 'galaxycommunitysession'
        # Currently this method does nothing unless session_cookie is 'galaxysession'
        if session_cookie == 'galaxysession' and self.galaxy_session.user is None:
            # TODO: re-engineer to eliminate the use of allowed_paths
            # as maintenance overhead is far too high.
            allowed_paths = (
                url_for( controller='root', action='index' ),
                url_for( controller='root', action='tool_menu' ),
                url_for( controller='root', action='masthead' ),
                url_for( controller='root', action='history' ),
                url_for( controller='user', action='api_keys' ),
                url_for( controller='user', action='create' ),
                url_for( controller='user', action='index' ),
                url_for( controller='user', action='login' ),
                url_for( controller='user', action='logout' ),
                url_for( controller='user', action='manage_user_info' ),
                url_for( controller='user', action='set_default_permissions' ),
                url_for( controller='user', action='reset_password' ),
                url_for( controller='user', action='openid_auth' ),
                url_for( controller='user', action='openid_process' ),
                url_for( controller='user', action='openid_associate' ),
                url_for( controller='library', action='browse' ),
                url_for( controller='history', action='list' ),
                url_for( controller='dataset', action='list' )
            )
            display_as = url_for( controller='root', action='display_as' )
            if self.app.config.ucsc_display_sites and self.request.path == display_as:
                try:
                    host = socket.gethostbyaddr( self.environ[ 'REMOTE_ADDR' ] )[0]
                except( socket.error, socket.herror, socket.gaierror, socket.timeout ):
                    host = None
                if host in UCSC_SERVERS:
                    return
            external_display_path = url_for( controller='dataset', action='display_application' )
            if self.request.path.startswith( external_display_path ):
                request_path_split = external_display_path.split( '/' )
                try:
                    if self.app.datatypes_registry.display_applications.get( request_path_split[-5] ) and request_path_split[-4] in self.app.datatypes_registry.display_applications.get( request_path_split[-5] ).links and request_path_split[-3] != 'None':
                        return
                except IndexError:
                    pass
            if self.request.path not in allowed_paths:
                self.response.send_redirect( url_for( controller='root', action='index' ) )

    def __create_new_session( self, prev_galaxy_session=None, user_for_new_session=None ):
        """
        Create a new GalaxySession for this request, possibly with a connection
        to a previous session (in `prev_galaxy_session`) and an existing user
        (in `user_for_new_session`).

        Caller is responsible for flushing the returned session.
        """
        session_key = self.security.get_new_guid()
        galaxy_session = self.app.model.GalaxySession(
            session_key=session_key,
            is_valid=True,
            remote_host = self.request.remote_host,
            remote_addr = self.request.remote_addr,
            referer = self.request.headers.get( 'Referer', None ) )
        if prev_galaxy_session:
            # Invalidated an existing session for some reason, keep track
            galaxy_session.prev_session_id = prev_galaxy_session.id
        if user_for_new_session:
            # The new session should be associated with the user
            galaxy_session.user = user_for_new_session
        return galaxy_session

    def get_or_create_remote_user( self, remote_user_email ):
        """
        Create a remote user with the email remote_user_email and return it
        """
        if not self.app.config.use_remote_user:
            return None

        user = self.sa_session.query( self.app.model.User ) \
                              .filter( self.app.model.User.table.c.email==remote_user_email ) \
                              .first()
        if user:
            # GVK: June 29, 2009 - This is to correct the behavior of a previous bug where a private
            # role and default user / history permissions were not set for remote users.  When a
            # remote user authenticates, we'll look for this information, and if missing, create it.
            if not self.app.security_agent.get_private_user_role( user ):
                self.app.security_agent.create_private_user_role( user )
            if 'webapp' not in self.environ or self.environ['webapp'] != 'tool_shed':
                if not user.default_permissions:
                    self.app.security_agent.user_set_default_permissions( user )
                    self.app.security_agent.user_set_default_permissions( user, history=True, dataset=True )
        elif user is None:
            username = remote_user_email.split( '@', 1 )[0].lower()
            random.seed()
            user = self.app.model.User( email=remote_user_email )
            user.set_password_cleartext( ''.join( random.sample( string.letters + string.digits, 12 ) ) )
            user.external = True
            # Replace invalid characters in the username
            for char in filter( lambda x: x not in string.ascii_lowercase + string.digits + '-', username ):
                username = username.replace( char, '-' )
            # Find a unique username - user can change it later
            if ( self.sa_session.query( self.app.model.User ).filter_by( username=username ).first() ):
                i = 1
                while ( self.sa_session.query( self.app.model.User ).filter_by( username=(username + '-' + str(i) ) ).first() ):
                    i += 1
                username += '-' + str(i)
            user.username = username
            self.sa_session.add( user )
            self.sa_session.flush()
            self.app.security_agent.create_private_user_role( user )
            # We set default user permissions, before we log in and set the default history permissions
            if 'webapp' not in self.environ or self.environ['webapp'] != 'tool_shed':
                self.app.security_agent.user_set_default_permissions( user )
            #self.log_event( "Automatically created account '%s'", user.email )
        return user

    def __update_session_cookie( self, name='galaxysession' ):
        """
        Update the session cookie to match the current session.
        """
        self.set_cookie( self.security.encode_guid(self.galaxy_session.session_key ),
                         name=name, path=self.app.config.cookie_path )

    def handle_user_login( self, user ):
        """
        Login a new user (possibly newly created)

           - create a new session
           - associate new session with user
           - if old session had a history and it was not associated with a user, associate it with the new session,
             otherwise associate the current session's history with the user
           - add the disk usage of the current session to the user's total disk usage
        """
        # Set the previous session
        prev_galaxy_session = self.galaxy_session
        prev_galaxy_session.is_valid = False
        # Define a new current_session
        self.galaxy_session = self.__create_new_session( prev_galaxy_session, user )
        if self.webapp.name == 'galaxy':
            cookie_name = 'galaxysession'
            # Associated the current user's last accessed history (if exists) with their new session
            history = None
            try:
                users_last_session = user.galaxy_sessions[0]
                last_accessed = True
            except:
                users_last_session = None
                last_accessed = False
            if (prev_galaxy_session.current_history and not
                    prev_galaxy_session.current_history.deleted and
                    prev_galaxy_session.current_history.datasets):
                if prev_galaxy_session.current_history.user is None or prev_galaxy_session.current_history.user == user:
                    # If the previous galaxy session had a history, associate it with the new
                    # session, but only if it didn't belong to a different user.
                    history = prev_galaxy_session.current_history
                    if prev_galaxy_session.user is None:
                        # Increase the user's disk usage by the amount of the previous history's datasets if they didn't already own it.
                        for hda in history.datasets:
                            user.total_disk_usage += hda.quota_amount( user )
            elif self.galaxy_session.current_history:
                history = self.galaxy_session.current_history
            if (not history and users_last_session and
                    users_last_session.current_history and not
                    users_last_session.current_history.deleted):
                history = users_last_session.current_history
            elif not history:
                history = self.get_history( create=True )
            if history not in self.galaxy_session.histories:
                self.galaxy_session.add_history( history )
            if history.user is None:
                history.user = user
            self.galaxy_session.current_history = history
            if not last_accessed:
                # Only set default history permissions if current history is not from a previous session
                self.app.security_agent.history_set_default_permissions( history, dataset=True, bypass_manage_permission=True )
            self.sa_session.add_all( ( prev_galaxy_session, self.galaxy_session, history ) )
        else:
            cookie_name = 'galaxycommunitysession'
            self.sa_session.add_all( ( prev_galaxy_session, self.galaxy_session ) )
        self.sa_session.flush()
        # This method is not called from the Galaxy reports, so the cookie will always be galaxysession
        self.__update_session_cookie( name=cookie_name )


    def handle_user_logout( self, logout_all=False ):
        """
        Logout the current user:
           - invalidate the current session
           - create a new session with no user associated
        """
        prev_galaxy_session = self.galaxy_session
        prev_galaxy_session.is_valid = False
        self.galaxy_session = self.__create_new_session( prev_galaxy_session )
        self.sa_session.add_all( ( prev_galaxy_session, self.galaxy_session ) )
        galaxy_user_id = prev_galaxy_session.user_id
        if logout_all and galaxy_user_id is not None:
            for other_galaxy_session in self.sa_session.query( self.app.model.GalaxySession ) \
                                            .filter( and_( self.app.model.GalaxySession.table.c.user_id==galaxy_user_id,
                                                                self.app.model.GalaxySession.table.c.is_valid==True,
                                                                self.app.model.GalaxySession.table.c.id!=prev_galaxy_session.id ) ):
                other_galaxy_session.is_valid = False
                self.sa_session.add( other_galaxy_session )
        self.sa_session.flush()
        if self.webapp.name == 'galaxy':
            # This method is not called from the Galaxy reports, so the cookie will always be galaxysession
            self.__update_session_cookie( name='galaxysession' )
        elif self.webapp.name == 'tool_shed':
            self.__update_session_cookie( name='galaxycommunitysession' )

    def get_galaxy_session( self ):
        """
        Return the current galaxy session
        """
        return self.galaxy_session

    def get_history( self, create=False ):
        """
        Load the current history, creating a new one only if there is not
        current history and we're told to create.
        Transactions will not always have an active history (API requests), so
        None is a valid response.
        """
        history = None
        if self.galaxy_session:
            history = self.galaxy_session.current_history
        if not history and util.string_as_bool( create ):
            history = self.new_history()
        return history

    def set_history( self, history ):
        if history and not history.deleted:
            self.galaxy_session.current_history = history
        self.sa_session.add( self.galaxy_session )
        self.sa_session.flush()

    history = property( get_history, set_history )

    def new_history( self, name=None ):
        """
        Create a new history and associate it with the current session and
        its associated user (if set).
        """
        # Create new history
        history = self.app.model.History()
        if name:
            history.name = name
        # Associate with session
        history.add_galaxy_session( self.galaxy_session )
        # Make it the session's current history
        self.galaxy_session.current_history = history
        # Associate with user
        if self.galaxy_session.user:
            history.user = self.galaxy_session.user
        # Track genome_build with history
        history.genome_build = util.dbnames.default_value
        # Set the user's default history permissions
        self.app.security_agent.history_set_default_permissions( history )
        # Save
        self.sa_session.add_all( ( self.galaxy_session, history ) )
        self.sa_session.flush()
        return history

    def get_current_user_roles( self ):
        user = self.get_user()
        if user:
            roles = user.all_roles()
        else:
            roles = []
        return roles

    def user_is_admin( self ):
        if self.api_inherit_admin:
            return True
        admin_users = [ x.strip() for x in self.app.config.get( "admin_users", "" ).split( "," ) ]
        return self.user and admin_users and self.user.email in admin_users

    def user_can_do_run_as( self ):
        run_as_users = self.app.config.get( "api_allow_run_as", "" ).split( "," )
        return self.user and run_as_users and self.user.email in run_as_users

    def get_toolbox(self):
        """Returns the application toolbox"""
        return self.app.toolbox

    @base.lazy_property
    def template_context( self ):
        return dict()

    @property
    def model( self ):
        return self.app.model

    def make_form_data( self, name, **kwargs ):
        rval = self.template_context[name] = FormData()
        rval.values.update( kwargs )
        return rval

    def set_message( self, message, type=None ):
        """
        Convenience method for setting the 'message' and 'message_type'
        element of the template context.
        """
        self.template_context['message'] = message
        if type:
            self.template_context['status'] = type

    def get_message( self ):
        """
        Convenience method for getting the 'message' element of the template
        context.
        """
        return self.template_context['message']

    def show_message( self, message, type='info', refresh_frames=[], cont=None, use_panels=False, active_view="" ):
        """
        Convenience method for displaying a simple page with a single message.

        `type`: one of "error", "warning", "info", or "done"; determines the
                type of dialog box and icon displayed with the message

        `refresh_frames`: names of frames in the interface that should be
                          refreshed when the message is displayed
        """
        return self.fill_template( "message.mako", status=type, message=message, refresh_frames=refresh_frames, cont=cont, use_panels=use_panels, active_view=active_view )

    def show_error_message( self, message, refresh_frames=[], use_panels=False, active_view="" ):
        """
        Convenience method for displaying an error message. See `show_message`.
        """
        return self.show_message( message, 'error', refresh_frames, use_panels=use_panels, active_view=active_view )

    def show_ok_message( self, message, refresh_frames=[], use_panels=False, active_view="" ):
        """
        Convenience method for displaying an ok message. See `show_message`.
        """
        return self.show_message( message, 'done', refresh_frames, use_panels=use_panels, active_view=active_view )

    def show_warn_message( self, message, refresh_frames=[], use_panels=False, active_view="" ):
        """
        Convenience method for displaying an warn message. See `show_message`.
        """
        return self.show_message( message, 'warning', refresh_frames, use_panels=use_panels, active_view=active_view )

    def show_form( self, form, header=None, template="form.mako", use_panels=False, active_view="" ):
        """
        Convenience method for displaying a simple page with a single HTML
        form.
        """
        return self.fill_template( template, form=form, header=header, use_panels=( form.use_panels or use_panels ),
                                    active_view=active_view )

    def fill_template(self, filename, **kwargs):
        """
        Fill in a template, putting any keyword arguments on the context.
        """
        # call get_user so we can invalidate sessions from external users,
        # if external auth has been disabled.
        self.get_user()
        if filename.endswith( ".mako" ):
            return self.fill_template_mako( filename, **kwargs )
        else:
            template = Template( file=os.path.join(self.app.config.template_path, filename),
                                 searchList=[kwargs, self.template_context, dict(caller=self, t=self, h=helpers, util=util, request=self.request, response=self.response, app=self.app)] )
            return str( template )

    def fill_template_mako( self, filename, **kwargs ):
        template = self.webapp.mako_template_lookup.get_template( filename )
        template.output_encoding = 'utf-8'
        data = dict( caller=self, t=self, trans=self, h=helpers, util=util, request=self.request, response=self.response, app=self.app )
        data.update( self.template_context )
        data.update( kwargs )
        return template.render( **data )

    def stream_template_mako( self, filename, **kwargs ):
        template = self.webapp.mako_template_lookup.get_template( filename )
        template.output_encoding = 'utf-8'
        data = dict( caller=self, t=self, trans=self, h=helpers, util=util, request=self.request, response=self.response, app=self.app )
        data.update( self.template_context )
        data.update( kwargs )
        ## return template.render( **data )
        def render( environ, start_response ):
            response_write = start_response( self.response.wsgi_status(), self.response.wsgi_headeritems() )
            class StreamBuffer( object ):
                def write( self, d ):
                    response_write( d.encode( 'utf-8' ) )
            buffer = StreamBuffer()
            context = mako.runtime.Context( buffer, **data )
            template.render_context( context )
            return []
        return render

    def fill_template_string(self, template_string, context=None, **kwargs):
        """
        Fill in a template, putting any keyword arguments on the context.
        """
        template = Template( source=template_string,
                             searchList=[context or kwargs, dict(caller=self)] )
        return str(template)

    @property
    def db_builds( self ):
        """
        Returns the builds defined by galaxy and the builds defined by
        the user (chromInfo in history).
        """
        dbnames = list()
        datasets = self.sa_session.query( self.app.model.HistoryDatasetAssociation ) \
                                  .filter_by( deleted=False, history_id=self.history.id, extension="len" )
        for dataset in datasets:
            dbnames.append( (dataset.dbkey, dataset.name) )
        user = self.get_user()
        if user and 'dbkeys' in user.preferences:
            user_keys = from_json_string( user.preferences['dbkeys'] )
            for key, chrom_dict in user_keys.iteritems():
                dbnames.append((key, "%s (%s) [Custom]" % (chrom_dict['name'], key) ))
        dbnames.extend( util.dbnames )
        return dbnames

    @property
    def ucsc_builds( self ):
        return util.dlnames['ucsc']

    @property
    def ensembl_builds( self ):
        return util.dlnames['ensembl']

    @property
    def ncbi_builds( self ):
        return util.dlnames['ncbi']

    @property
    def user_ftp_dir( self ):
        identifier = self.app.config.ftp_upload_dir_identifier
        return os.path.join( self.app.config.ftp_upload_dir, getattr(self.user, identifier) )

    def db_dataset_for( self, dbkey ):
        """
        Returns the db_file dataset associated/needed by `dataset`, or `None`.
        """
        # If no history, return None.
        if self.history is None:
            return None
        if isinstance(self.history, Bunch):
            # The API presents a Bunch for a history.  Until the API is
            # more fully featured for handling this, also return None.
            return None
        datasets = self.sa_session.query( self.app.model.HistoryDatasetAssociation ) \
                                  .filter_by( deleted=False, history_id=self.history.id, extension="len" )
        for ds in datasets:
            if dbkey == ds.dbkey:
                return ds
        return None

    def request_types(self):
        if self.sa_session.query( self.app.model.RequestType ).filter_by( deleted=False ).count() > 0:
            return True
        return False


class FormBuilder( object ):
    """
    Simple class describing an HTML form
    """
    def __init__( self, action="", title="", name="form", submit_text="submit", use_panels=False ):
        self.title = title
        self.name = name
        self.action = action
        self.submit_text = submit_text
        self.inputs = []
        self.use_panels = use_panels

    def add_input( self, type, name, label, value=None, error=None, help=None, use_label=True  ):
        self.inputs.append( FormInput( type, label, name, value, error, help, use_label ) )
        return self

    def add_text( self, name, label, value=None, error=None, help=None  ):
        return self.add_input( 'text', label, name, value, error, help )

    def add_password( self, name, label, value=None, error=None, help=None  ):
        return self.add_input( 'password', label, name, value, error, help )

    def add_select( self, name, label, value=None, options=[], error=None, help=None, use_label=True ):
        self.inputs.append( SelectInput( name, label, value=value, options=options, error=error, help=help, use_label=use_label   ) )
        return self


class FormInput( object ):
    """
    Simple class describing a form input element
    """
    def __init__( self, type, name, label, value=None, error=None, help=None, use_label=True ):
        self.type = type
        self.name = name
        self.label = label
        self.value = value
        self.error = error
        self.help = help
        self.use_label = use_label


class SelectInput( FormInput ):
    """ A select form input. """
    def __init__( self, name, label, value=None, options=[], error=None, help=None, use_label=True ):
        FormInput.__init__( self, "select", name, label, value=value, error=error, help=help, use_label=use_label )
        self.options = options


class FormData( object ):
    """
    Class for passing data about a form to a template, very rudimentary, could
    be combined with the tool form handling to build something more general.
    """
    def __init__( self ):
        self.values = Bunch()
        self.errors = Bunch()


class Bunch( dict ):
    """
    Bunch based on a dict
    """
    def __getattr__( self, key ):
        if key not in self: raise AttributeError, key
        return self[key]
    def __setattr__( self, key, value ):
        self[key] = value

