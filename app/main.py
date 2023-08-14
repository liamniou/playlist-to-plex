import eyed3
import ffmpeg
import httpx
import logging as log
import httpx
import os
import paramiko
import pathlib
import setlist_fm_client
import signal
import spotipy
import sys
import telebot


from dataclasses import dataclass, field
from pathvalidate import sanitize_filename
from plexapi.myplex import PlexServer
from spotipy.oauth2 import SpotifyClientCredentials
from telebot import types
from typing import List
from yt_dlp import YoutubeDL


@dataclass
class Setlist:
    id: str
    artist_name: str
    country: str
    event_date: str
    sets: dict
    plex_playlist_name: str = field(init=False)
    songs: List = field(default_factory=lambda: [])
    songs_on_plex: List = field(default_factory=lambda: [])

    def __post_init__(self):
        self.event_date = self.event_date.split("-")[-1]
        self.songs = [song["name"] for set in self.sets["set"] for song in set["song"]]
        self.plex_playlist_name = (
            f"{self.artist_name} - {self.event_date} - {self.country}"
        )


@dataclass
class SpotifyPlaylist:
    playlist_name: str
    songs: dict
    playlist_by_artist: dict = field(default_factory=lambda: {})
    songs_on_plex_by_artist: dict = field(default_factory=lambda: {})

    def __post_init__(self):
        for song in self.songs:
            if not self.songs_on_plex_by_artist.get(
                song["track"]["artists"][0]["name"]
            ):
                self.songs_on_plex_by_artist[song["track"]["artists"][0]["name"]] = []

        for song in self.songs:
            if self.playlist_by_artist.get(song["track"]["artists"][0]["name"]):
                self.playlist_by_artist[song["track"]["artists"][0]["name"]].append(
                    song["track"]["name"]
                )
            else:
                self.playlist_by_artist[song["track"]["artists"][0]["name"]] = [
                    song["track"]["name"]
                ]


@dataclass
class YouTubeConversation:
    chat_id: str
    video_link: str
    artist_name: str
    song: str


AUTHORIZED_USERS = [
    int(x) for x in os.getenv("AUTHORIZED_USERS", "294967926,191151492").split(",")
]
PLEX_HOST = os.getenv("PLEX_HOST")
PLEX_HOST_USERNAME = os.getenv("PLEX_HOST_USERNAME")
PLEX_TOKEN = os.getenv("PLEX_TOKEN")
PLEX_LIBRARY_NAME = os.getenv("PLEX_LIBRARY_NAME", "Music")
PLEX_UPDATE_SCRIPT_PATH = os.getenv("PLEX_UPDATE_SCRIPT_PATH", False)
SETLIST_API_KEY = os.getenv("SETLIST_FM_API_KEY")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "/sshconfig/id_rsa.oci")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/Plex/downloads/ytdl")
REMOTE_DOWNLOAD_DIR = os.getenv(
    "REMOTE_DOWNLOAD_DIR", "/Users/admin/Plex/downloads/ytdl"
)
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")


bot = telebot.TeleBot(
    os.getenv("TELEGRAM_BOT_TOKEN"),
    threaded=False,
    parse_mode="Markdown",
)


plex = PlexServer("http://192.168.0.37:32400", PLEX_TOKEN)
plex_music = plex.library.section(PLEX_LIBRARY_NAME)


def signal_handler(signal_number):
    print("Received signal " + str(signal_number) + ". Trying to end tasks and exit...")
    bot.stop_polling()
    sys.exit(0)


def log_and_send_message_decorator(fn):
    def wrapper(message):
        bot.send_message(message.chat.id, f"Executing your command, please wait...")
        log.info("[FROM {}] [{}]".format(message.chat.id, message.text))
        if message.chat.id in AUTHORIZED_USERS:
            reply = fn(message)
        else:
            reply = "Sorry, this is a private bot"
        log.info("[TO {}] [{}]".format(message.chat.id, reply))
        try:
            bot.send_message(message.chat.id, reply)
        except Exception as e:
            log.warning(f"Something went wrong:\n{e}")
            bot.send_message(
                message.chat.id, "Sorry, I can't send you reply. Report it to @Lestarby"
            )

    return wrapper


def get_setlist_id_from_url(setlist_url: str) -> str:
    return setlist_url.split("/")[-1].split("-")[-1].replace(".html", "")


def parse_setlistfm_url(setlistfm_url):
    response = setlist_fm_client.get_setlist(
        get_setlist_id_from_url(setlistfm_url),
        api_key=SETLIST_API_KEY,
    )

    return Setlist(
        id=response.json()["id"],
        artist_name=response.json()["artist"]["name"],
        country=response.json()["venue"]["city"]["country"]["code"],
        event_date=response.json()["eventDate"],
        sets=response.json()["sets"],
    )


def search_plex_by_artist(wanted_songs, artist_name):
    plex_search = plex_music.searchArtists(title=artist_name)

    plex_songs = []
    for artist in plex_search:
        for album in artist.albums():
            for song in album.tracks():
                plex_songs.append(song)

    wanted_songs_on_plex = []
    sorted_plex_songs = []
    for song in wanted_songs:
        log.info(f"Searching for song '{artist_name} - {song}' in Plex library")
        for plex_song in plex_songs:
            print(
                f"Comparing {song} with {plex_song.title}: ",
                song.lower().replace("'", "’") == plex_song.title.lower(),
            )
            if song.lower().replace("'", "’") == plex_song.title.lower():
                sorted_plex_songs.append(plex_song)
                wanted_songs_on_plex.append(song)
                break

    return sorted_plex_songs, wanted_songs_on_plex


def create_plex_playlist(songs, playlist_name):
    for playlist in plex.playlists():
        if playlist.title == playlist_name:
            log.warning(f"Playlist {playlist_name} already exists. Deleting...")
            playlist.delete()
            break
    plex.createPlaylist(
        title=playlist_name,
        items=songs,
    )
    return True


def create_plex_playlist_from_setlist(setlist):
    sorted_plex_songs, wanted_songs_on_plex = search_plex_by_artist(
        setlist.songs, setlist.artist_name
    )
    setlist.songs_on_plex = wanted_songs_on_plex

    if sorted_plex_songs:
        create_plex_playlist(
            sorted_plex_songs,
            f"{setlist.artist_name} - {setlist.event_date} - {setlist.country}",
        )
        return True

    return False


def create_plex_playlist_from_spotify_playlist(playlist):
    all_artists_sorted_plex_songs = []
    for artist, songs in playlist.playlist_by_artist.items():
        sorted_plex_songs, wanted_songs_on_plex = search_plex_by_artist(songs, artist)
        playlist.songs_on_plex_by_artist[artist].extend(wanted_songs_on_plex)
        all_artists_sorted_plex_songs.extend(sorted_plex_songs)

    if all_artists_sorted_plex_songs:
        create_plex_playlist(
            all_artists_sorted_plex_songs,
            f"{playlist.name}",
        )
        return True

    return False


def get_album_by_song_name(song_name, artist_name):
    log.info(f"Searching for album of {artist_name} - {song_name}")
    response = httpx.get(
        f"https://api.deezer.com/search?q=artist:'{artist_name}' track:'{song_name}'"
    )
    if response.status_code == 200:
        response_json = response.json()
        if response_json["total"] > 0:
            return response_json["data"][0]["album"]["title"]
        else:
            return None
    else:
        log.error(f"Error while searching for album of {artist_name} - {song_name}")
        return None


def set_song_id3_tags(song, artist_name, file):
    audiofile = eyed3.load(file)
    audiofile.initTag()
    audiofile.tag.artist = artist_name
    audiofile.tag.album_artist = artist_name
    audiofile.tag.album = get_album_by_song_name(song, artist_name)
    audiofile.tag.title = song
    audiofile.tag.save()


def get_video_object_from_yt_search(ytdl_opts, yt_search_string):
    with YoutubeDL(ytdl_opts) as ydl:
        return ydl.extract_info(yt_search_string, download=False)


def download_from_yt(song_name, yt_search_string):
    try:
        os.mkdir(DOWNLOAD_DIR)
    except FileExistsError:
        pass
    song_name = sanitize_filename(song_name)
    log.info(f"STARTING: download {song_name} from YouTube")

    ytdl_opts = {
        "outtmpl": f"{song_name}.%(ext)s",
        "format": "bestaudio",
        "noplaylist": True,
    }
    video = get_video_object_from_yt_search(ytdl_opts, yt_search_string)

    with YoutubeDL(ytdl_opts) as ydl:
        result = ydl.download(video.get("webpage_url", None))

    file = sorted(pathlib.Path(".").glob(f"{song_name}.*"))[0]

    if result == 0 and os.path.isfile(file):
        log.info(f"SUCCESS: {file} was downloaded")

        ext = os.path.splitext(file)[1]
        target_file = os.path.join(DOWNLOAD_DIR, f"{file}".replace(ext, ".mp3"))

        log.info(f"STARTING: convert {file} to {target_file}")
        try:
            (ffmpeg.input(f"/app/{file}").output(target_file).overwrite_output().run())
            os.remove(file)
            log.info(f"SUCCESS: convert {file} to {target_file}")
            return target_file
        except ffmpeg.Error as e:
            print(e.stderr, file=sys.stderr)

    else:
        log.warning(f"FAILURE: can't find downloaded file of {song_name}")

    return None


def download_missing_songs_from_yt(artist_name, missing_songs, chat_id):
    downloaded_songs = []

    for song in missing_songs:
        try:
            downloaded_file = download_from_yt(
                song, f"ytsearch:'{artist_name} - {song}'"
            )

            if downloaded_file:
                downloaded_songs.append(downloaded_file)
                log.info(f"Setting metadata of {downloaded_file}")
                set_song_id3_tags(song, artist_name, downloaded_file)
            else:
                bot.send_message(chat_id, f"Failed to download missing song: {song}")

        except Exception as e:
            log.error(f"Error while processing {song}: {e}")
            bot.send_message(chat_id, f"Failed to download missing song: {song}")

    return downloaded_songs


def update_plex(category):
    if PLEX_UPDATE_SCRIPT_PATH:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            PLEX_HOST, username=PLEX_HOST_USERNAME, key_filename=SSH_KEY_PATH, port=2830
        )
        ssh.exec_command(f"{PLEX_UPDATE_SCRIPT_PATH} {REMOTE_DOWNLOAD_DIR} {category}")
    else:
        log.warning("Can't update Plex because PLEX_UPDATE_SCRIPT_PATH is not set")


@bot.message_handler(
    func=lambda m: m.text is not None
    and m.text.startswith(("https://www.setlist.fm/setlist"))
)
@log_and_send_message_decorator
def create_from_setlistfm(message):
    response = ""
    setlist = parse_setlistfm_url(message.text)
    playlist = create_plex_playlist_from_setlist(setlist)
    if playlist:
        response = "Playlist created!"
    missing_songs = list(set(setlist.songs) - set(setlist.songs_on_plex))
    if missing_songs:
        bot.send_message(message.chat.id, f"Missing songs: {missing_songs}")
        downloaded_songs = download_missing_songs_from_yt(
            setlist.artist_name, missing_songs, message.chat.id
        )
        if downloaded_songs:
            update_plex("Music")
            response += (
                "\nThere were missing songs, re-send the link to get full playlist"
            )

    return response


@bot.message_handler(
    func=lambda m: m.text is not None
    and m.text.startswith(tuple(["https://www.youtube.com", "https://youtu.be"]))
)
def download_youtube_video(message):
    if message.chat.id not in AUTHORIZED_USERS:
        bot.reply_to(message, "Sorry, this is a private bot")
        return

    video = get_video_object_from_yt_search(
        {"format": "bestaudio", "noplaylist": True}, message.text
    )
    artist_name = video.get("channel", "N/A")
    song = (
        video.get("title", "N/A")
        .replace(artist_name, "")
        .replace("Official Video", "")
        .replace("[]", "")
        .replace("()", "")
        .replace("-", "")
        .strip()
    )

    conversation = YouTubeConversation(
        chat_id=message.chat.id,
        video_link=message.text,
        artist_name=artist_name,
        song=song,
    )

    msg = bot.reply_to(
        message,
        f"Enter artist name manually or press the button with the suggested one",
        reply_markup=types.ReplyKeyboardMarkup().add(
            types.KeyboardButton(conversation.artist_name)
        ),
    )
    bot.register_next_step_handler(
        msg, lambda m: process_artist_name_step(m, conversation)
    )


def process_artist_name_step(message, conversation):
    conversation.artist_name = message.text
    msg = bot.reply_to(
        message,
        "Enter song name manually or press the button with the suggested one",
        reply_markup=types.ReplyKeyboardMarkup().add(
            types.KeyboardButton(conversation.song)
        ),
    )
    bot.register_next_step_handler(msg, lambda m: process_song_name(m, conversation))


def process_song_name(message, conversation):
    conversation.song = message.text

    markup = types.ReplyKeyboardMarkup()
    markup.add(
        types.KeyboardButton("Music"),
        types.KeyboardButton("Podcast"),
        types.KeyboardButton("Audiobook"),
    )

    msg = bot.reply_to(
        message, "Do I sort it as music/podcast/audiobook?", reply_markup=markup
    )
    bot.register_next_step_handler(
        msg, lambda m: process_category_and_download(m, conversation)
    )


def process_category_and_download(message, conversation):
    markup = types.ReplyKeyboardRemove(selective=False)
    category = message.text

    try:
        bot.reply_to(message, f"Starting the download...", reply_markup=markup)
        downloaded_file = download_from_yt(conversation.song, conversation.video_link)

        if downloaded_file:
            log.info(f"Setting metadata of {downloaded_file}")
            set_song_id3_tags(
                conversation.song,
                conversation.artist_name,
                downloaded_file,
            )
            bot.send_message(
                conversation.chat_id,
                f"{conversation.artist_name} - {conversation.song} was downloaded",
            )
            update_plex(category)
        else:
            bot.send_message(
                conversation.chat_id, "Something went wrong during the download"
            )

    except Exception as e:
        log.error(f"Error while processing {conversation.song}: {e}")
        bot.send_message(
            conversation.chat_id,
            f"Failed to download missing song: {conversation.song}",
        )


def parse_spotify_url(url):
    playlist_id = url.split("/")[-1].split("?")[0]
    sp = spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
        )
    )
    playlist = sp.playlist(f"spotify:playlist:{playlist_id}")
    return SpotifyPlaylist(sp["name"], playlist["tracks"]["items"])


@bot.message_handler(
    func=lambda m: m.text is not None
    and m.text.startswith(("https://open.spotify.com/playlist"))
)
@log_and_send_message_decorator
def create_from_spotify(message):
    response = ""
    spotify_playlist = parse_spotify_url(message.text)
    playlist = create_plex_playlist_from_spotify_playlist(spotify_playlist)

    if playlist:
        response = "Playlist created!"

    missing_songs = {}

    for artist, songs in spotify_playlist.songs_on_plex_by_artist.items():
        artist_missings_songs = list(
            set(songs) - set(spotify_playlist.songs_on_plex_by_artist[artist])
        )
        missing_songs[artist] = artist_missings_songs

    if missing_songs:
        bot.send_message(message.chat.id, f"Missing songs: {missing_songs}")
        for artist, missing_songs in missing_songs.items():
            downloaded_songs = download_missing_songs_from_yt(
                artist, missing_songs, message.chat.id
            )

        if downloaded_songs:
            update_plex("Music")
            response += (
                "\nThere were missing songs, re-send the link to get full playlist"
            )

    return response


def main():
    log.basicConfig(level=log.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Bot was started.")
    signal.signal(signal.SIGINT, signal_handler)
    log.info("Starting bot polling...")
    bot.polling()


if __name__ == "__main__":
    main()
