import os
from flask import Flask, flash, request, redirect, url_for, render_template
from werkzeug.utils import secure_filename
from lobby import Lobby, display_lobby
from flask_socketio import SocketIO, emit, send
import db
import time
from parser_tools import parse_log, generate_key, parse_hero_stats
from keygen import generate_access_key
from pymongo.errors import DuplicateKeyError
import configparser
from leaderboard import get_top_x_role, get_top_x_overall
from user import User, get_user_by_discord, get_all_users, adjust_team_rating, update_player_hero_stats, end_game
from flask_discord import DiscordOAuth2Session, requires_authorization, Unauthorized
import requests
import json
from match import add_match, display_match
from flask_cors import CORS
from playerqueue import get_players_in_queue, add_to_queue, matchmake_3, matchmake_3_ow2, can_start, can_start_ow2, \
    remove_from_queue
from threading import Lock
from ow_info import MAPS, COLUMNS, STAT_COLUMNS

REDIRECT_TO = "http://127.0.0.1:3000/login"
ORIGINS = ["http://127.0.0.1:3000"]

app = Flask(__name__)
cors = CORS(app, supports_credentials=True, origins=ORIGINS)
# Get Config Data

config = configparser.ConfigParser()
config.read("config.ini")
CLIENT_ID = config.get("DISCORD", "CLIENT_ID")
CLIENT_SECRET = config.get("DISCORD", "CLIENT_SECRET")
CALLBACK = config.get("DISCORD", "CALLBACK")
# https://stackoverflow.com/questions/54892779/how-to-serve-a-local-app-using-waitress-and-nginx
# Generate Flask Secret key for auth
key = generate_key()
app.secret_key = key

# Setting up the config for the discord auth.
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "true"
app.config["DISCORD_CLIENT_ID"] = CLIENT_ID
app.config["DISCORD_CLIENT_SECRET"] = CLIENT_SECRET
app.config["DISCORD_REDIRECT_URI"] = CALLBACK

# Setting up the config for the file storage.
LOG_FOLDER = "log_folder"
app.config['UPLOAD_FOLDER'] = LOG_FOLDER
ALLOWED_EXTENSIONS = {'txt'}

# Threading config
thread = None
thread_lock = Lock()

# Set up sockets

socketio = SocketIO(app, cors_allowed_origins="*", logger=True)

# Set up OAuth session
discord = DiscordOAuth2Session(app)

players_connected = {}


def allowed_file(filename):
    """
    If the file has an extension and the extension is in our list of allowed extensions, then return True
    :param filename: The name of the file that was uploaded
    :return: a boolean value.
    """
    """Checks if our file is of the correct type"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/')
def landing():
    """
    It redirects the user to the post_upload page
    :return: A redirect to the post_upload page.
    """

    return redirect(url_for("get_upload"))


@app.route('/ping')
def ping():
    """
    > This function returns the string "OK" and a status code of 200
    :return: A tuple with the string "OK" and the integer 200.
    """
    return "OK", 200


@app.post('/upload/<lobby_id>')
def post_upload(lobby_id):
    """
    It takes the file that was uploaded, parses it, and adds it to the database
    :return: the string 'Error?!'
    """
    if 'file' not in request.files:
        return "", 400
    file = request.files['file']
    if file.filename == '':
        return "", 400
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        user = discord.fetch_user()
        winner = request.form["teams"]
        print(winner)
        data = parse_log(file.filename)
        scoreboard = data[2][data[0]]
        lobby_details = display_lobby(lobby_id)
        hero_stats = parse_hero_stats(data[2])
        # Puts the time played into the stats
        for k, value in data[1].items():
            for hero, time_played in value.items():
                hero_stats[k][hero]["time"] = time_played
        for player_name in hero_stats.keys():
            print(f"Updating {player_name}")
            update_player_hero_stats(player_name, hero_stats[player_name])
        state = add_match(file.filename,
                          scoreboard,
                          winner,
                          lobby_details["host"],
                          str(lobby_id),
                          lobby_details["team_1"],
                          lobby_details["team_2"])
        if state != -1:
            adjust_team_rating(lobby_details["team_1"], lobby_details["team_2"], winner)
            lobby_details["finished"] = True
            db.lobbies.update_one({"_id": lobby_details["id"]}, update={"$set": lobby_details})
            # Iterating through the list of players in the lobby and assigning them a value of 1, 0, or -1 depending on
            # whether they won, lost, or tied.
            for player in (lobby_details["team_1"] + lobby_details["team_2"]):
                outcome = 0
                if player in lobby_details["team_1"] and winner == "1":
                    outcome = 1
                if player in lobby_details["team_1"] and winner == "2":
                    outcome = -1
                if player in lobby_details["team_2"] and winner == "2":
                    outcome = 1
                if player in lobby_details["team_2"] and winner == "1":
                    outcome = -1
                end_game(player["bnet"], outcome)

        return "", 201
    return "", 415


@app.route("/login")
def login():
    """
    Redirect to discord auth
    :return: A redirect to the discord auth page.
    """
    return discord.create_session()


@app.route("/auth")
def callback():
    """
    It redirects the user to the login page.
    :return: The user object
    """
    discord.callback()
    user = discord.fetch_user()
    # Here's the redirect!
    return redirect(REDIRECT_TO)


@app.errorhandler(Unauthorized)
def redirect_unauthorized(e):
    """
    If the user is not logged in, redirect them to the login page

    :param e: The exception that was raised
    :return: A redirect to the login page.
    """
    return "Unauthorized", 401


@app.route("/user")
@requires_authorization
def curr_user():
    """
    If the user is in our database, we render the user page. If not, we redirect them to the signup page
    :return: The user object
    """
    user = discord.fetch_user()
    this_user = db.users.find_one({"_id": str(user)})
    print(this_user)
    if this_user is None:
        # If the user isn't in our database, we make them signup
        return {"userStatus": None}, 403
    return {"userStatus": "Created", "user": this_user}, 200


@app.route("/users")
@requires_authorization
def get_users():
    """
    `get_users` returns a list of all users and a status code of 200
    :return: A list of all users in the database.
    """
    return {"users": get_all_users()}, 200


@app.route("/users/name/<discord_name>")
@requires_authorization
def get_user_discord(discord_name: str):
    """
    `get_user` takes a discord name and returns the user's information

    :param discord_name: The discord name of the user you want to get
    :type discord_name: str
    :return: A JSON object containing the user's information.
    """
    try:
        return db.users.find_one({"_id": discord_name.replace("-", "#")}), 200
    except Exception as e:
        return "Can't find user", 500


@app.route("/users/id/<id>")
@requires_authorization
def get_user_by_id(id):
    """
    `get_user` takes a discord name and returns the user's information
    :param discord_name: The discord name of the user you want to get
    :type discord_name: str
    :return: A JSON object containing the user's information.
    """
    user = db.users.find_one({"id": int(id)})
    if user is not None:
        return user
    else:
        return "Can't find user", 500


@app.get("/signup")
@requires_authorization
def signup():
    """
    > This function renders the signup page, which links a user's discord account to their battle.net account
    :return: The user's discord name and id
    """
    """Signup page, links bnet to discord"""
    user = discord.fetch_user()
    return render_template("signup.html", discord_name=str(user), id=user.id)


@app.post("/signup")
@requires_authorization
def post_signup():
    user = discord.fetch_user()
    user_id = user.id
    name = str(user)
    # Through requests we get user information
    bnet = request.form["bnet"]
    try:
        key = request.form["key"]
    except Exception:
        # No key
        return "", 401
    if not key == generate_access_key(int(user_id)):
        # No key
        return "", 401
    discord_name = name
    avatar = user.avatar_url
    roles = []

    if request.form["tank"]:
        roles.append("TANK")
    if request.form["dps"]:
        roles.append("DPS")
    if request.form["support"]:
        roles.append("support")


    # We now get the players ranks
    try:
        role_ranks = {}
        # Getting the player's rank for each role from the API.
        ranks = json.loads(requests.get(f"https://ovrstat.com/stats/pc/{str(bnet).replace('#', '-')}").text)[
            "ratings"]
        for rank in ranks:
            print(f"Role {rank['role']}: {rank['level']}")
            role_ranks[rank["role"]] = rank['level']
    except TypeError:
        # Handle the NoneType error from iterating an empty element, ie the player hasn't placed
        print(f"Type Error {str(bnet).replace('#', '-')}")
        role_ranks = {'tank': 0, 'damage': 0, 'support': 0}
    except Exception:
        # Handle any other error, this bnet is busted probably
        # We should do something else here, but not sure what yet
        print(f"Other Error {str(bnet).replace('#', '-')}")
        # TODO
        role_ranks = {'tank': 0, 'damage': 0, 'support': 0}
    print(roles)
    user = discord.fetch_user()
    # Placeholder new user object for now
    # As the user class automatically stores the user in the database
    try:
        new_user = User(discord_name, bnet, roles, avatar, user_id, user.name, role_ranks)
    except Exception as e:
        print(e)
        return "", 500
    return "", 201


@app.get('/upload')
def get_upload():
    """
    It returns the HTML template for the upload page
    :return: The upload_log.html file is being returned.
    """
    """Route for uploading logs to server"""
    return render_template("upload_log.html")


@app.get('/game_log/<log>')
@requires_authorization
def log(log):
    """
    It takes a log file, parses it, and returns a rendered template with the parsed data

    :param log: The name of the log file
    :return: The log.html template is being returned.
    """
    """Displays the stats from the selected log"""
    user = discord.fetch_user()
    data = parse_log(log)
    scoreboard = data[2][data[0]]
    out = {"scoreboard": scoreboard, "player_heroes": data[1], "log": log}
    return out, 200


@app.get('/game_logs')
def logs():
    """
    It returns a list of all the files in the `LOG_FOLDER` directory
    :return: A dictionary with the list of matches and a status code of 200
    """
    """Shows all games"""
    uploaded_matches = os.listdir(LOG_FOLDER)
    print(uploaded_matches)
    return {"matches": uploaded_matches}, 200


@app.get('/game_log/<log>/<player>')
@requires_authorization
def match_player_hero_stats(log, player):
    """
    It takes a log file and a player name, and returns a rendered template with the player's hero stats

    :param log: the log file to be parsed
    :param player: The player's name
    :return: A dictionary of the hero stats for the given player in the given match.
    """
    """Displays Hero stats for given player in given match"""
    data = parse_log(log)
    hero_stats = parse_hero_stats(data[2])[player]
    out = {"hero_stats": hero_stats, "player_heroes": data[1][player], "player": player}
    return out, 200


@app.get('/queue_ow1/<role>')
@requires_authorization
def queue_player_ow1(role: str):
    """
    It takes a discord user id, and a role, and adds them to the queue. If the queue is full, it starts a match

    :param role: str
    :type role: str
    :return: The return is a tuple of two values. The first value is the response body, and the second value is the status
    code.
    """
    try:
        user_to_queue = discord.fetch_user()
        queue_state = get_players_in_queue()
        user_bnet = get_user_by_discord(str(user_to_queue)).bnet_name
        add_to_queue(user_bnet, role)
        if can_start:
            # TODO websocket stuff
            # start queue
            team_1, team_2 = matchmake_3()
            return {"team_1": team_1, "team_2": team_2}, 200
        return {"players_in_queue": queue_state}, 200

    except DuplicateKeyError as e:
        print(e)
        print(type(e))
        return "", 500


@app.get('/queue_ow2/<role>')
@requires_authorization
def queue_player_ow2(role: str):
    """
    It queues a player for a role in Overwatch 2.

    :param role: str
    :type role: str
    :return: The return is a tuple of two values. The first value is the response body, and the second value is the status
    code.
    """
    try:
        user_to_queue = discord.fetch_user()
        queue_state = get_players_in_queue()
        user_bnet = get_user_by_discord(str(user_to_queue)).bnet_name
        add_to_queue(user_bnet, role)
        if can_start_ow2:
            # TODO websocket stuff
            # start queue
            team_1, team_2 = matchmake_3_ow2()
            return {"team_1": team_1, "team_2": team_2}, 200
        return {"players_in_queue": queue_state}

    except DuplicateKeyError as e:
        print(e)
        print(type(e))
        return "", 500


@app.get("/leaderboard/<role>")
def get_top_x_by_role(role):
    """
    It returns the top x (default 10) players for a given role

    :param role: The role you want to get the top players for
    :return: A list of the top 10 (or whatever number is specified) players in the specified role.
    """
    top = request.args.get("top", 10)
    return get_top_x_role(top, role), 200


@app.get("/leaderboard")
def get_top_x():
    """
    It returns the top 10 (or whatever number is specified in the `top` query parameter) users for the given role

    :param role: The role you want to get the top players for
    :return: A list of the top 10 (or whatever number is specified) users by overall rating.
    """
    top = request.args.get("top", 10)
    return get_top_x_overall(top), 200


@app.get("/lobby/<id>")
@requires_authorization
def get_lobby(id):
    """
    `get_lobby` returns the lobby with the given id

    :param id: The id of the lobby you want to get
    :return: The display_lobby function is being returned.
    """
    return display_lobby(id), 200


@app.get("/match/<id>")
@requires_authorization
def get_match(id):
    """
    It returns the match that is identified by the id parameter

    :param id: The id of the match you want to get
    :return: The function display_match is being returned.
    """
    return display_match(id), 200


@app.get("/user_stats/<discord_name>")
def get_player_stats(discord_name):
    """
    This function takes in a discord name and returns the hero stats of that player.

    :param discord_name: The discord name of the player you want to get the stats of
    :return: A dictionary of hero stats
    """
    hero_stats = db.users.find_one({"_id": discord_name})["hero_stats"]
    return hero_stats


@app.get("/hero_leaderboard/<hero>/<statname>")
def hero_stat_leaderboard(hero, statname):
    """
    > Get all players who have played the hero, sort them by the stat, and return the top X

    :param hero: The hero you want to get the leaderboard for
    :param statname: The name of the stat you want to sort by
    :return: A list of players who have played the hero and have the stat.
    """
    top = request.args.get("top", 10)
    player_list = list(db.users.find())
    # Get all players with stats
    # This could be better
    player_list = [player for player in player_list if player.get("hero_stats", False)]
    # get all players who have played that specific hero
    player_list = [player for player in player_list if player["hero_stats"].get(hero, False)]
    player_list = sorted(player_list, key=lambda i: i["hero_stats"][hero][statname], reverse=True)
    return {f"top {top}": player_list[:top]}


@socketio.on_error()
def error_helper(error):
    print(error)


@socketio.on('connect')
def socket_connect(json):
    """
    It takes a JSON object, and then adds the player's name and socket ID to a dictionary

    :param json: The JSON object that is sent from the client
    """
    print("Connected")

    players_connected[json["player"]] = request.sid
    print(f"Player:{json['player']} - SID:{request.sid} Connected")
    emit({"Connected": "True"})


@socketio.on('disconnect')
def socket_disconnect():
    """
    It removes the user from the queue on socket disconnect
    """
    user = discord.fetch_user()
    remove_from_queue(str(user))
    print(f"{str(user)} Disconnected")


@socketio.on('queue')
def socket_queue(json):
    """
    It takes in a json object, adds the player to the queue, and if there are enough players in the queue, it will create a
    new lobby and send a message to all the players in the lobby

    :param json: The json object that is sent from the client
    """
    players_connected[json["player"]] = request.sid
    emit({"queue_status": "In Queue", "Role": json["role"]})
    print("Adding")
    add_to_queue(json['player'], json["role"])
    if can_start:
        team_1, team_2 = matchmake_3()
        new_lobby = Lobby(team_1, team_2)
        emit("pop", {"match_id": new_lobby.lobby_name, "players": team_1 + team_2}, Broadcast=True)


if __name__ == '__main__':
    print("Running - ")
    socketio.run(app, port=5000, host='0.0.0.0')
