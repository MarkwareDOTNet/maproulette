from maproulette import app
from flask.ext.restful import reqparse, fields, marshal,\
marshal_with, Api, Resource
from flask.ext.restful.fields import get_value, Raw
from flask import session, make_response
from maproulette.helpers import GeoPoint, get_challenge_or_404, \
    get_random_task, osmlogin_required
from maproulette.models import Challenge, Task, Action, db
from geoalchemy2.functions import ST_Buffer, ST_AsGeoJSON
from shapely import geometry
import geojson
import json  

class ProtectedResource(Resource):
    method_decorators = [osmlogin_required]

class PointField(Raw):
    def format(self, value):
        app.logger.debug(type(value))
        return {}

challenge_summary = {
    'slug':         fields.String,
    'title':        fields.String
}

challenge_details = {
    'description':  fields.String,
    'blurb':        fields.String,
    'help':         fields.String,
    'instruction':  fields.String,
    'active':       fields.Boolean,
    'difficulty':   fields.Integer,
}

task_fields = {
    'id':           fields.String(attribute='identifier'),
    'text':         fields.String(attribute='instruction')
}

api = Api(app)

#override the default JSON representation to support GeoJSON
@api.representation('application/json')
def output_json(data, code, headers=None):
    if isinstance(data, geometry.base.BaseGeometry):
        app.logger.debug('this is a geo element')
        resp = make_response(geojson.dumps(data), code)
    else:
        app.logger.debug('this is a non geo element')
        app.logger.debug(data)
        resp = make_response(json.dumps(data), code)
    resp.headers.extend(headers or {})
    return resp

class ApiChallengeList(ProtectedResource):
    method_decorators = [osmlogin_required]
    @marshal_with(challenge_summary)
    def get(self):
        """returns a list of challenges.
        Optional URL parameters are:
        difficulty: the desired difficulty to filter on (1=easy, 2=medium, 3=hard)
        contains: the coordinate to filter on (as lon|lat, returns only
        challenges whose bounding polygons contain this point)
        example: /api/c/challenges?contains=-100.22|40.45&difficulty=2
        """
        app.logger.debug('retrieving list of challenges')

        # initialize the parser
        parser = reqparse.RequestParser()
        parser.add_argument('difficulty', type=int, choices=["1","2","3"],
                            help='difficulty cannot be parsed')
        parser.add_argument('contains', type=GeoPoint,
                            help="Could not parse contains")
        args = parser.parse_args()
        
        # Try to get difficulty from argument, or users prefers or default
        difficulty = args['difficulty'] or session.get('difficulty') or 1
        
        # Try to get location from argument or user prefs
        contains = None

        if args['contains']:
            contains = args['contains']
            coordWKT = 'POINT(%s %s)' % (contains.lat, contains.lon)
        elif 'home_location' in session:
            contains = session['home_location']
            coordWKT = 'POINT(%s %s)' % tuple(contains.split("|"))
            app.logger.debug('home location retrieved from session')

        # get the list of challenges meeting the criteria
        query = db.session.query(Challenge)

        if difficulty:
            query = query.filter(Challenge.difficulty==difficulty)
        if contains:
            query = query.filter(Challenge.geom.ST_Contains(coordWKT))

        challenges = [challenge for challenge in query.all() 
        if challenge.active]
        
        #if there are no near challenges, return anything
        if len(challenges) == 0:
            query = db.session.query(Challenge)
            app.logger.debug('we have nothing close, looking all over within difficulty setting')
            challenges = [challenge for challenge in query.filter(
                Challenge.difficulty==difficulty).all()
                if challenge.active]
                          

        app.logger.debug('we still have nothing, returning any challenge')

        # what if we still don't get anything? get anything!
        if len(challenges) == 0:
            query = db.session.query(Challenge)
            challenges = [challenge
            for challenge in query.all()
            if challenge.active]
        return challenges

class ApiChallengeDetail(ProtectedResource):
    @marshal_with(challenge_details)
    def get(self, slug):
        app.logger.debug('retrieving challenge %s' % (slug,))
        return get_challenge_or_404(slug)

class ApiChallengePolygon(ProtectedResource):
    def get(self, slug):
        app.logger.debug('retrieving challenge %s' % (slug,))
        challenge = get_challenge_or_404(slug)
        geometry = challenge.geometry
        app.logger.debug(geojson.dumps(geometry))
        return geometry
        
class ApiChallengeStats(ProtectedResource):
    def get(self, slug):
        challenge = get_challenge_or_404(slug, True)
        
        total = Task.query.filter(slug == challenge.slug).count()
        tasks = Task.query.filter(slug == challenge.slug).all()
        osmid = session.get('osm_id')
        available = len([task for task in tasks
                         if challenge.task_available(task, osmid)])
        
        app.logger.info("{user} requested challenge stats for {challenge}".format(
                user=osmid, challenge=slug))
                
        return {'total': total, 'available': available}

class ApiChallengeTask(ProtectedResource):
    def get(self, slug):
        "Returns a task for specified challenge"
        challenge = get_challenge_or_404(slug, True)
        parser = reqparse.RequestParser()
        parser.add_argument('num', type=int, default=1,
                            help='Number of return results cannot be parsed')
        parser.add_argument('near', type=GeoPoint,
                            help='Near argument could not be parsed')
        parser.add_argument('assign', type=int, default=1,
                            help='Assign could not be parsed')
        args = parser.parse_args()
        osmid = session.get('osm_id')
        assign = args['assign']
        near = args['near']
        
        app.logger.info("{user} requesting task from {challenge} near {near} assiging: {assign}".format(user=osmid, challenge=slug, near=near, assign=assign))

        task = None
        if near:
            coordWKT = 'POINT(%s %s)' % (near.lat, near.lon)
            task_query = Task.query.filter(Task.location.ST_Intersects(
                    ST_Buffer(coordWKT, app.config["NEARBUFFER"]))).limit(1)
            task_list = [task for task in task_query
                         if challenge.task_available(task, osmid)]
        if not near or not task:
            # If no location is specified, or no tasks were found, gather
            # random tasks
            task = get_random_task(challenge)
            # If no tasks are found with this method, then this challenge
            # is complete
        if not task:
            # Is this the right error?
            osmerror("ChallengeComplete",
                     "Challenge {} is complete".format(slug))
        if assign:
            app.logger.debug('assigning task')
            task.actions.append(Action("assigned", osmid))
            db.session.add(task)
            
        db.session.commit()
            
        app.logger.debug("task found matching criteria")
        
        return marshal(task, task_fields)

class ApiChallengeTaskGeometries(ProtectedResource):
    def get(self, slug, identifier):
        task = Task.query.filter(
            Task.challenge_slug == slug).filter(
            Task.identifier == identifier).all()[-1]
        if task is None:
            abort(404) # FIXME is this the right error code?
        app.logger.debug(task.geometries)
        return task.geometries

api.add_resource(ApiChallengeList, '/api/challenges/')
api.add_resource(ApiChallengeDetail, '/api/challenge/<string:slug>')
api.add_resource(ApiChallengePolygon, '/api/challenge/<string:slug>/geometry')
api.add_resource(ApiChallengeStats, '/api/challenge/<string:slug>/stats')
api.add_resource(ApiChallengeTask, '/api/challenge/<slug>/task')
api.add_resource(ApiChallengeTaskGeometries, '/api/challenge/<slug>/task/<identifier>/geom')