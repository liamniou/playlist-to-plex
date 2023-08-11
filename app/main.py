import eyed3
import httpx
import logging as log
import httpx
import os
import paramiko
import pathlib
import setlist_fm_client
import signal
import sys
import subprocess
import telebot


from dataclasses import dataclass, field
from plexapi.myplex import PlexServer
from telebot import types
from typing import List


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
class YouTubeConversation:
    chat_id: str
    video_link: str
    artist_name: str = field(init=False)
    song: str = field(init=False)


AUTHORIZED_USERS = [
    int(x) for x in os.getenv("AUTHORIZED_USERS", "294967926,191151492").split(",")
]
PLEX_HOST = os.getenv("PLEX_HOST")
PLEX_HOST_USERNAME = os.getenv("PLEX_HOST_USERNAME")
PLEX_TOKEN = os.getenv("PLEX_TOKEN")
PLEX_LIBRARY_NAME = os.getenv("PLEX_LIBRARY_NAME", "Music")
PLEX_UPDATE_SCRIPT_PATH = os.getenv(
    "PLEX_UPDATE_SCRIPT_PATH", False
)
SETLIST_API_KEY = os.getenv("SETLIST_FM_API_KEY")
SSH_KEY_PATH = os.getenv("SSH_KEY_PATH", "/sshconfig/id_rsa.oci")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/Plex/downloads/ytdl")
REMOTE_DOWNLOAD_DIR = os.getenv("REMOTE_DOWNLOAD_DIR", "/Users/admin/Plex/downloads/ytdl")

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

    print(response.json())

    return Setlist(
        id=response.json()["id"],
        artist_name=response.json()["artist"]["name"],
        country=response.json()["venue"]["city"]["country"]["code"],
        event_date=response.json()["eventDate"],
        sets=response.json()["sets"],
    )


def create_plex_playlist_from_setlist(setlist):
    plex_search = plex_music.searchArtists(title=setlist.artist_name)

    plex_songs = []
    for artist in plex_search:
        for album in artist.albums():
            for song in album.tracks():
                plex_songs.append(song)

    sorted_plex_songs = []
    for song in setlist.songs:
        log.info(f"Searching for song '{setlist.artist_name} - {song}' in Plex library")
        for plex_song in plex_songs:
            print(
                f"Comparing {song} with {plex_song.title}: ",
                song.lower().replace("'", "’") == plex_song.title.lower(),
            )
            if song.lower().replace("'", "’") == plex_song.title.lower():
                sorted_plex_songs.append(plex_song)
                setlist.songs_on_plex.append(song)
                break

    if sorted_plex_songs:
        for playlist in plex.playlists():
            if playlist.title == setlist.plex_playlist_name:
                log.warning(
                    f"Playlist {setlist.plex_playlist_name} already exists. Deleting..."
                )
                playlist.delete()
                break
        plex.createPlaylist(
            title=f"{setlist.artist_name} - {setlist.event_date} - {setlist.country}",
            items=sorted_plex_songs,
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


def download_from_yt(song_name, yt_search_string):
    yt_command = f"yt-dlp {yt_search_string} -f bestaudio --max-downloads 1 -o '{song_name}.%(ext)s'"

    log.info(f"STARTING: download {song_name} from YouTube")
    process = subprocess.Popen(
        yt_command,
        shell=True,
    )
    process.wait()

    file = sorted(pathlib.Path(".").glob(f"{song_name}.*"))[0]

    if os.path.isfile(file):
        log.info(f"SUCCESS: {file} was downloaded")        

        ext = os.path.splitext(file)[1]
        target_file = f"{file}".replace(ext, ".mp3")

        log.info(f"STARTING: convert {file} to {target_file}")
        process = subprocess.Popen(
            f"ffmpeg -i '/app/{file}' '{DOWNLOAD_DIR}/{target_file}' -y",
            shell=True,
        )
        process.wait()
        log.info(process.returncode)
        os.remove(file)
        if process.returncode == 0:
            log.info(f"SUCCESS: convert {file} to {target_file}{target_file}")
            return target_file
    else:
        log.warning(f"FAILURE: can't find downloaded file of{song_name}")

    return None


def download_missing_songs_from_yt(setlist, missing_songs, chat_id):
    downloaded_songs = []
    try:
        os.mkdir(DOWNLOAD_DIR)
    except FileExistsError:
        pass
    for song in missing_songs:
        try:
            downloaded_file = download_from_yt(song, f"ytsearch:'{setlist.artist_name} - {song}'")

            if downloaded_file:
                downloaded_songs.append(downloaded_file)
                log.info(f"Setting metadata of {downloaded_file}")
                set_song_id3_tags(song, setlist.artist_name, f"{DOWNLOAD_DIR}/{downloaded_file}")
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
        ssh.exec_command(
            f"{PLEX_UPDATE_SCRIPT_PATH} {REMOTE_DOWNLOAD_DIR} {category}"
        )
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
            setlist, missing_songs, message.chat.id
        )
        if downloaded_songs:
            update_plex("Music")
            response += (
                "\nThere were missing songs, re-send the link to get full playlist"
            )

    return response


@bot.message_handler(
    func=lambda m: m.text is not None
    and m.text.startswith(("https://www.youtube.com"))
)
def download_youtube_video(message):
    if message.chat.id not in AUTHORIZED_USERS:
        bot.reply_to(message, "Sorry, this is a private bot")
        return

    conversation = YouTubeConversation(
        chat_id=message.chat.id,
        video_link=message.text,
    )
    msg = bot.reply_to(
        message,
        f"Enter artist name",
    )
    bot.register_next_step_handler(msg, lambda m: process_artist_name_step(m, conversation))


def process_artist_name_step(message, conversation):
    conversation.artist_name = message.text
    msg = bot.reply_to(message, 'Enter song name')
    bot.register_next_step_handler(msg, lambda m: process_song_name(m, conversation))


def process_song_name(message, conversation):
    conversation.song = message.text

    markup = types.ReplyKeyboardMarkup()
    button_1 = types.KeyboardButton("Music")
    button_2 = types.KeyboardButton("Podcast")
    button_3 = types.KeyboardButton("Audiobook")
    markup.add(button_1, button_2, button_3)

    msg = bot.reply_to(message, 'Do I sort it as music or podcast?', reply_markup=markup)
    bot.register_next_step_handler(msg, lambda m: process_category_and_download(m, conversation))


def process_category_and_download(message, conversation):
    markup = types.ReplyKeyboardRemove(selective=False)
    category = message.text

    try:
        bot.reply_to(message, f"Starting the download...", reply_markup=markup)
        downloaded_file = download_from_yt(conversation.song, conversation.video_link)

        if downloaded_file:
            log.info(f"Setting metadata of {downloaded_file}")
            set_song_id3_tags(conversation.song, conversation.artist_name, f"{DOWNLOAD_DIR}/{downloaded_file}")
            bot.send_message(conversation.chat_id, f"{conversation.artist_name} - {conversation.song} was downloaded")
            update_plex(category)
        else:
            bot.send_message(conversation.chat_id, "Something went wrong during the download")

    except Exception as e:
        log.error(f"Error while processing {conversation.song}: {e}")
        bot.send_message(conversation.chat_id, f"Failed to download missing song: {conversation.song}")


def main():
    log.basicConfig(level=log.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Bot was started.")
    signal.signal(signal.SIGINT, signal_handler)
    log.info("Starting bot polling...")
    bot.polling()


if __name__ == "__main__":
    main()
