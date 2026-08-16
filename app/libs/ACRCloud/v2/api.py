from ..result import Result
from ..song import Song
import requests
from json import loads
from datetime import datetime, timedelta
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.settings import get_settings
from app.database.session import get_session


from typing import List
from sqlalchemy.orm import Session


class ACRCloudAPI:
    """
    This class uses the Console API of the ACR Cloud to scan uploaded music files
    that might contain traces of other tracks.

    The functions that are implemented in this class are a wrapper for the
    file scanning API calls.

    For reference, see https://docs.acrcloud.com/reference/console-api
    """

    base_url = "https://api-v2.acrcloud.com/api"
    spotify_base_url = "https://open.spotify.com/track/"
    applemusic_base_url = ""
    deezer_base_url = "https://www.deezer.com/en/track/"
    youtube_base_url = "https://www.youtube.com/watch?v="

    def __init__(self, access_key, access_secret, bearer_token, container_id):
        """Creates an instance of the ACRCloudAPI-class:

        Parameters:
            access_key (str): The access key that will be used to access the Identification API.
            access_secret (str): The access secret that will be used to access the Identification API.
            bearer_token (str): The token that will be used to access the Console API.

        Returns:
            The ACRCloudAPI-object.
        """

        # save variables for communicating with the api

        self.access_key = access_key
        self.access_secret = access_secret
        self.bearer_token = bearer_token
        self.container_id = container_id

    def _get_spotify_album_art(self, spotify_track_id):
        """Fetch album art from Spotify using track ID.

        Parameters:
            spotify_track_id (str): The Spotify track ID.

        Returns:
            str: The album art URL, or empty string if not found.
        """
        try:
            from app.misc import get_spotify_access_token

            spotify_token = get_spotify_access_token()
            if not spotify_token:
                return ""

            headers = {"Authorization": f"Bearer {spotify_token}"}
            url = f"https://api.spotify.com/v1/tracks/{spotify_track_id}"

            response = requests.get(url, headers=headers)
            if response.ok:
                track_data = response.json()
                album_images = track_data.get("album", {}).get("images", [])
                if album_images:
                    return album_images[0].get("url", "")
        except Exception as e:
            print(f"Error fetching album art for Spotify track {spotify_track_id}: {e}")

        return ""

    def uploadFile(self, filepath: str, filename: str = "") -> Song:
        """Uploads the file to be analyzed to the container of the user.

        Parameters:
            filepath (str): The full path to the file.
            container_id (int): The ACRCloud container ID.

        Returns:
            dict: The Song object.
        """

        endpoint = f"/fs-containers/{self.container_id}/files"

        payload = {"data_type": "audio"}
        if filename:
            payload["name"] = filename

        files = [("file", (filepath, open(filepath, "rb"), "application/octet-stream"))]
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "POST", self.base_url + endpoint, headers=headers, data=payload, files=files
        )
        data = loads(response.text)["data"]
        return Song(
            filename=data["name"],
            file_id=data["id"],
            tracks=[],
            loading=data["state"] == 0,
        )

    def uploadUrl(self, db: Session):
        pass

    def uploadFingerprint(self, db: Session):
        pass

    def showContainerContents(
        self, file_ids: List[str], page: int = 1, per_page: int = 10
    ):

        print(f"[ACRCloud] Total file_ids: {len(file_ids)}, page: {page}, per_page: {per_page}")

        if len(file_ids) == 0:
            return {"songs": [], "current_page": 1, "last_page": 1, "per_page": 0}

        # Calculate pagination BEFORE calling ACRCloud API
        # ACRCloud returns all requested files, so we need to paginate the file_ids ourselves
        total_files = len(file_ids)
        total_pages = max(1, (total_files + per_page - 1) // per_page)  # Ceiling division

        # Slice file_ids for the requested page
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_file_ids = file_ids[start_idx:end_idx]

        print(f"[ACRCloud] Paginated: showing {len(paginated_file_ids)} files (indices {start_idx}-{end_idx})")

        if len(paginated_file_ids) == 0:
            return {"songs": [], "current_page": page, "last_page": total_pages, "per_page": per_page}

        files = ",".join(paginated_file_ids)
        endpoint = f"/fs-containers/{self.container_id}/files/{files}"

        payload = {}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "GET", self.base_url + endpoint, headers=headers, data=payload
        )

        if response.status_code == 200:
            # this contains an array with every uploaded song
            data = response.json()
            arr = data["data"]

            # Fetch file results in parallel for much faster loading
            # This reduces 10 sequential requests (~10s) to parallel (~1-2s)
            songs = []
            file_ids_to_fetch = [file["id"] for file in arr]

            with ThreadPoolExecutor(max_workers=10) as executor:
                # Submit all requests in parallel
                future_to_file_id = {
                    executor.submit(self.showFileResults, file_id): file_id
                    for file_id in file_ids_to_fetch
                }

                # Collect results maintaining order
                results_by_id = {}
                for future in as_completed(future_to_file_id):
                    file_id = future_to_file_id[future]
                    try:
                        song = future.result()
                        results_by_id[file_id] = song.toJSON()
                    except Exception as e:
                        print(f"[ACRCloud] Error fetching results for {file_id}: {e}")
                        results_by_id[file_id] = None

                # Preserve original order
                for file_id in file_ids_to_fetch:
                    if results_by_id.get(file_id):
                        songs.append(results_by_id[file_id])

            return {
                "songs": songs,
                "current_page": page,
                "last_page": total_pages,
                "per_page": per_page,
            }
        else:
            raise ValueError(f"API returned the following error:\n{response.text}")

    def deleteFile(self, file_id):
        """Deletes a file in a container.

        Parameters:
            file_id (str): The ID of the file.
            container_id (str): The ID of the container.

        Returns:
            Bool: True if successful, false else.
        """

        endpoint = f"/fs-containers/{self.container_id}/files/{file_id}"

        payload = {}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "DELETE", url=self.base_url + endpoint, headers=headers, data=payload
        )

        # response code 204: Deleted successfully
        if response.status_code != 204:
            return False
            raise ValueError(f"API returned the following error:\n{response.text}")
        return True

    def showFileResults(self, file_id):
        """Show all the tracks that have been recognized in the file.

        Returns:
            List<Result>: A list of Result-objects that could be found.
        """
        endpoint = f"/fs-containers/{self.container_id}/files/{file_id}"

        payload = {}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "GET", self.base_url + endpoint, headers=headers, data=payload
        )

        # data is theoretically an array with results (?). It should only have one entry if the file_id is given
        # Every entry itself has an array which are the actual results of one file (?)
        data = response.json()["data"]  # if file_id is not given
        track_list = []

        for track in data:
            if track["results"]:
                # there are two types of results, music and cover song
                if "music" in track["results"]:
                    for found_music in track["results"]["music"]:

                        # ignore the song if it is too old since it is probably not relevant

                        try:

                            release_date = datetime.strptime(
                                found_music["result"]["release_date"], "%Y-%m-%d"
                            ).date()
                            five_years_date = (
                                datetime.now() - timedelta(days=5 * 365)
                            ).date()

                            if five_years_date > release_date:
                                continue
                        except:
                            pass

                        # extract variables that will be put in to the result object

                        # mandatory variables first; these have their keys in the dictionary, so no try catch needed

                        type = "music"
                        artist_name = found_music["result"]["artists"][0]["name"]
                        song_name = found_music["result"]["title"]
                        duration = found_music["result"].get("duration_ms", 0) / 1000

                        # optional variables might not have the keys in the dictionary, so use try catch

                        isrc = ""
                        spotify_link = ""
                        spotify_track_id = ""
                        applemusic_link = ""
                        deezer_link = ""
                        youtube_link = ""
                        album_art = ""

                        try:
                            isrc = found_music["result"]["external_ids"]["isrc"]
                        except (KeyError, TypeError):
                            isrc = ""

                        try:
                            spotify_track_id = found_music["result"][
                                "external_metadata"
                            ]["spotify"]["track"]["id"]
                            spotify_link = f"{self.spotify_base_url}{spotify_track_id}"
                        except (KeyError, TypeError):
                            spotify_link = ""

                        try:
                            applemusic_link = f"{self.applemusic_base_url}{found_music['result']['external_metadata']['applemusic']['track']['id']}"
                        except (KeyError, TypeError):
                            applemusic_link = ""

                        try:
                            deezer_link = f"{self.deezer_base_url}{found_music['result']['external_metadata']['deezer']['track']['id']}"
                        except (KeyError, TypeError):
                            deezer_link = ""

                        try:
                            youtube_link = f"{self.youtube_base_url}{found_music['result']['external_metadata']['youtube']['vid']}"
                        except (KeyError, TypeError):
                            youtube_link = ""

                        # Fetch album art from Spotify if we have a track ID
                        if spotify_track_id:
                            album_art = self._get_spotify_album_art(spotify_track_id)

                        result = Result(
                            type=type,
                            artist_name=artist_name,
                            song_name=song_name,
                            duration=duration,
                            isrc=isrc,
                            spotify_link=spotify_link,
                            applemusic_link=applemusic_link,
                            deezer_link=deezer_link,
                            youtube_link=youtube_link,
                            album_art=album_art,
                        )
                        track_list.append(result)

                if "cover_songs" in track["results"]:
                    for found_music in track["results"]["cover_songs"]:

                        # extract variables that will be put in to the result object

                        # mandatory variables first; these have their keys in the dictionary, so no try catch needed

                        type = "cover_song"
                        artist_name = found_music["result"]["artists"][0]["name"]
                        song_name = found_music["result"]["title"]
                        duration = found_music["result"]["duration_ms"] / 1000

                        isrc = ""
                        spotify_link = ""
                        applemusic_link = ""
                        deezer_link = ""
                        youtube_link = ""

                        # optional variables might not have the keys in the dictionary, so use try catch

                        try:
                            isrc = found_music["result"]["external_ids"]["isrc"]
                        except KeyError:
                            isrc = ""

                        try:
                            spotify_link = f"{self.spotify_base_url}{found_music['result']['external_metadata']['spotify']['track']['id']}"
                        except KeyError:
                            spotify_link = ""

                        try:
                            applemusic_link = f"{self.spotify_base_url}{found_music['result']['external_metadata']['applemusic']['track']['id']}"
                        except KeyError:
                            applemusic_link = ""

                        try:
                            deezer_link = f"{self.deezer_base_url}{found_music['result']['external_metadata']['deezer']['track']['id']}"
                        except KeyError:
                            deezer_link = ""

                        try:
                            youtube_link = f"{self.youtube_base_url}{found_music['result']['external_metadata']['youtube']['vid']}"
                        except KeyError:
                            youtube_link = ""

                        result = Result(
                            type=type,
                            artist_name=artist_name,
                            song_name=song_name,
                            duration=duration,
                            isrc=isrc,
                            spotify_link=spotify_link,
                            applemusic_link=applemusic_link,
                            deezer_link=deezer_link,
                            youtube_link=youtube_link,
                        )
                        # print(found_music['result']['external_metadata'])
                        # print(result)
                        track_list.append(result)
        return Song(
            filename=data[0]["name"],
            file_id=file_id,
            tracks=track_list,
            loading=data[0]["state"] == 0,
        )

    def rescanFile(self, file_id):
        """Rescans a file to find tracks again.

        Parameters:
            file_id (str): The ID of the file.
            container_id (str): The ID of the container.

        Returns:
            Dict: The response as a dictionary.
        """

        endpoint = f"/fs-containers/{self.container_id}/files/{file_id}/rescan"

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request("PUT", self.base_url + endpoint, headers=headers)
        data = response.json()["data"]

        track_list = []

        for track in data:
            if track["results"]:
                # there are two types of results, music and cover song
                if "music" in track["results"]:
                    for found_music in track["results"]["music"]:

                        # ignore the song if it is too old since it is probably not relevant

                        try:

                            release_date = datetime.strptime(
                                found_music["result"]["release_date"], "%Y-%m-%d"
                            ).date()
                            five_years_date = (
                                datetime.now() - timedelta(days=5 * 365)
                            ).date()

                            if five_years_date > release_date:
                                continue
                        except:
                            pass

                        # extract variables that will be put in to the result object

                        # mandatory variables first; these have their keys in the dictionary, so no try catch needed

                        type = "music"
                        artist_name = found_music["result"]["artists"][0]["name"]
                        song_name = found_music["result"]["title"]
                        duration = found_music["result"].get("duration_ms", 0) / 1000

                        # optional variables might not have the keys in the dictionary, so use try catch

                        isrc = ""
                        spotify_link = ""
                        spotify_track_id = ""
                        applemusic_link = ""
                        deezer_link = ""
                        youtube_link = ""
                        album_art = ""

                        try:
                            isrc = found_music["result"]["external_ids"]["isrc"]
                        except (KeyError, TypeError):
                            isrc = ""

                        try:
                            spotify_track_id = found_music["result"][
                                "external_metadata"
                            ]["spotify"]["track"]["id"]
                            spotify_link = f"{self.spotify_base_url}{spotify_track_id}"
                        except (KeyError, TypeError):
                            spotify_link = ""

                        try:
                            applemusic_link = f"{self.applemusic_base_url}{found_music['result']['external_metadata']['applemusic']['track']['id']}"
                        except (KeyError, TypeError):
                            applemusic_link = ""

                        try:
                            deezer_link = f"{self.deezer_base_url}{found_music['result']['external_metadata']['deezer']['track']['id']}"
                        except (KeyError, TypeError):
                            deezer_link = ""

                        try:
                            youtube_link = f"{self.youtube_base_url}{found_music['result']['external_metadata']['youtube']['vid']}"
                        except (KeyError, TypeError):
                            youtube_link = ""

                        # Fetch album art from Spotify if we have a track ID
                        if spotify_track_id:
                            album_art = self._get_spotify_album_art(spotify_track_id)

                        result = Result(
                            type=type,
                            artist_name=artist_name,
                            song_name=song_name,
                            duration=duration,
                            isrc=isrc,
                            spotify_link=spotify_link,
                            applemusic_link=applemusic_link,
                            deezer_link=deezer_link,
                            youtube_link=youtube_link,
                            album_art=album_art,
                        )
                        track_list.append(result)

                if "cover_songs" in track["results"]:
                    for found_music in track["results"]["cover_songs"]:

                        # extract variables that will be put in to the result object

                        # mandatory variables first; these have their keys in the dictionary, so no try catch needed

                        type = "cover_song"
                        artist_name = found_music["result"]["artists"][0]["name"]
                        song_name = found_music["result"]["title"]
                        duration = found_music["result"]["duration_ms"] / 1000

                        isrc = ""
                        spotify_link = ""
                        applemusic_link = ""
                        deezer_link = ""
                        youtube_link = ""

                        # optional variables might not have the keys in the dictionary, so use try catch

                        try:
                            isrc = found_music["result"]["external_ids"]["isrc"]
                        except KeyError:
                            isrc = ""

                        try:
                            spotify_link = f"{self.spotify_base_url}{found_music['result']['external_metadata']['spotify']['track']['id']}"
                        except KeyError:
                            spotify_link = ""

                        try:
                            applemusic_link = f"{self.spotify_base_url}{found_music['result']['external_metadata']['applemusic']['track']['id']}"
                        except KeyError:
                            applemusic_link = ""

                        try:
                            deezer_link = f"{self.deezer_base_url}{found_music['result']['external_metadata']['deezer']['track']['id']}"
                        except KeyError:
                            deezer_link = ""

                        try:
                            youtube_link = f"{self.youtube_base_url}{found_music['result']['external_metadata']['youtube']['vid']}"
                        except KeyError:
                            youtube_link = ""

                        result = Result(
                            type=type,
                            artist_name=artist_name,
                            song_name=song_name,
                            duration=duration,
                            isrc=isrc,
                            spotify_link=spotify_link,
                            applemusic_link=applemusic_link,
                            deezer_link=deezer_link,
                            youtube_link=youtube_link,
                        )
                        # print(found_music['result']['external_metadata'])
                        # print(result)
                        track_list.append(result)
        return Song(
            filename=data[0]["name"],
            file_id=file_id,
            tracks=track_list,
            loading=data[0]["state"] == 0,
        )


@lru_cache
def get_acrcloud_api():
    settings = get_settings()
    return ACRCloudAPI(
        settings.acrcloud_access_key,
        settings.acrcloud_access_secret,
        settings.acrcloud_bearer_token,
        settings.acrcloud_container_id,
    )
