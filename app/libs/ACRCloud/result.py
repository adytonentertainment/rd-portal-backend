class Result:

    def __init__(
        self,
        type,
        artist_name,
        song_name,
        duration,
        isrc,
        spotify_link,
        applemusic_link,
        deezer_link,
        youtube_link,
        album_art="",
    ):
        self.type = type
        self.artist_name = artist_name
        self.song_name = song_name
        self.duration = duration
        self.isrc = isrc
        self.spotify_link = spotify_link
        self.applemusic_link = applemusic_link
        self.deezer_link = deezer_link
        self.youtube_link = youtube_link
        self.album_art = album_art

    def __repr__(self):
        return f"""{self.artist_name} - {self.song_name};
            Duration = {self.duration}s;
            ISRC = {self.isrc};
            Spotify = {self.spotify_link};
            Apple Music = {self.applemusic_link};
            Deezer = {self.deezer_link};
            Youtube = {self.youtube_link};"""

    def toJSON(self):
        return {
            "type": self.type,
            "artist_name": self.artist_name,
            "song_name": self.song_name,
            "duration": self.duration,
            "isrc": self.isrc,
            "spotify_link": self.spotify_link,
            "applemusic_link": self.applemusic_link,
            "deezer_link": self.deezer_link,
            "youtube_link": self.youtube_link,
            "album_art": self.album_art,
        }
