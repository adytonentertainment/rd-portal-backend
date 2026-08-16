from ..result import Result
from ..song import Song
import requests
from json import loads
from datetime import datetime, timedelta
from functools import lru_cache
from app.settings import get_settings


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

    def __init__(self, access_key, access_secret, bearer_token):
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

    def uploadFile(self, filepath, container_id, filename=""):
        """Uploads the file to be analyzed to the container of the user.

        Parameters:
            filepath (str): The full path to the file.
            container_id (int): The ACRCloud container ID.

        Returns:
            dict: The Song object.
        """

        endpoint = f"/fs-containers/{container_id}/files"

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

    def uploadUrl(self, url, container_id, name=""):
        """Pulls the file from the url to be analyzed to the container of the user.

        Parameters:
            url (str): An URL to the track.
            container_id (int): The ACRCloud container ID.

        Returns:
            dict: The Song object.
        """

        endpoint = f"/fs-containers/{container_id}/files"

        payload = {"data_type": "audio", "url": url}
        if name:
            payload["name"] = name

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "POST", self.base_url + endpoint, headers=headers, data=payload
        )
        # does not work
        data = loads(response.text)["data"]
        return Song(
            filename=data["name"],
            file_id=data["id"],
            tracks=[],
            loading=data["state"] == 0,
        )

    def uploadFingerprint(self, fingerprint, container_id):
        """Uploads the fingerprint to be analyzed to the container of the user.

        Parameters:
            fingerprint (str): The fingerprint of the file.
            container_id (int): The ACRCloud container ID.
        """

        endpoint = f"/fs-containers/{container_id}/files"

        payload = {"data_type": "fingerprint"}
        files = [
            ("file", (fingerprint, open(fingerprint, "rb"), "application/octet-stream"))
        ]
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "POST", self.base_url + endpoint, headers=headers, data=payload, files=files
        )

        return loads(response.text)

    def hasContainer(self, name: str) -> bool:
        """Checks if a container with the specified name exists."""
        return self.getContainerIdByName(name) != -1

    def createContainer(self, name: str, already_taken=True):
        """Creates a container for an user which can be used to upload music files.

        Parameters:
            name (str): The name of the container.
            already_taken (bool): Whether to claim the container when a container with the given
            name already exists. Defaults to true.

        Returns:
            int: The ID of the container that has been created.

        Raises:
            ValueError: Wrong or missing parameters, name already taken
        """

        endpoint = f"/fs-containers/"

        payload = {
            "region": "eu-west-1",  # might need to change region depending on the users location to improve upload speed
            "engine": 1,  # 1 is only audio fingerprinting, # 3 is audio fingerprinting and cover song identification
            "name": name,
            "buckets": [23],  # 23 is the id for the ACR Cloud Music bucket
            "audio_type": "linein",
            "policy": {
                "type": "traverse",
                "interval": 30,
                "rec_length": 30,
                "points": 3,
            },
            "callback_url": "",
        }

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        # if the data is a dictionary, use the json parameter, not data
        response = requests.request(
            method="POST", url=self.base_url + endpoint, headers=headers, json=payload
        )

        if response.status_code == 201:
            return response.json()["data"]["id"]
        elif response.status_code == 422 and already_taken:
            # fetch the container and return it
            return self.getContainerIdByName(name)
        else:
            raise ValueError(f"API returned the following error:\n{response.text}")

    def getAllContainers(self):
        """Gets every container linked with this account."""

        endpoint = f"/fs-containers"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            method="GET", url=self.base_url + endpoint, headers=headers
        )

        return response.json()

    def deleteContainer(self, container_id):

        endpoint = f"/fs-containers/{container_id}"

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            method="DELETE", url=self.base_url + endpoint, headers=headers
        )

        return response

    def changeContainer(self, container_id, rec_length, interval):

        endpoint = f"/fs-containers/{container_id}"
        name = self.getContainerName(container_id)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        payload = {
            "region": "eu-west-1",  # might need to change region depending on the users location to improve upload speed
            "engine": 1,  # 1 is only audio fingerprinting, # 3 is audio fingerprinting and cover song identification
            "name": name,
            "buckets": [23],  # 23 is the id for the ACR Cloud Music bucket
            "audio_type": "linein",
            "policy": {
                "type": "traverse",
                "interval": interval,
                "rec_length": rec_length,
                "points": 3,
            },
            "callback_url": "",
        }

        response = requests.request(
            method="PUT", url=self.base_url + endpoint, headers=headers, json=payload
        )

        return response

    def getContainer(self, container_id):
        """Gets the container with the specified ID.

        Parameter:
            str: The ID of the container.

        Returns:
            A JSON response of the API.
        """

        endpoint = f"/fs-containers/{container_id}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            method="GET", url=self.base_url + endpoint, headers=headers
        )

        return response.json()

    def getContainerIdByName(self, name):
        """Gets the container ID that has a matching name.

        Parameter:
            str: The name of the container.

        Returns:
            int: The container ID.
        """

        endpoint = f"/fs-containers?page=1&per_page=1000000"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            method="GET", url=self.base_url + endpoint, headers=headers
        )

        arr = response.json()["data"]

        for container in arr:
            if container["name"] == name:
                return container["id"]

        return -1

    def getContainerIds(self):
        """Gets the IDs of all containers.

        Returns:
            List<int>: A list with all container IDs.
        """

        endpoint = f"/fs-containers?page=1&per_page=100000"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            method="GET", url=self.base_url + endpoint, headers=headers
        )

        arr = response.json()["data"]

        containerId_list = []

        for container in arr:
            containerId_list.append(container["id"])

        return containerId_list

    def getContainerName(self, container_id):
        """Returns the name of a container.

        Parameters:
            container_id (int): The ID of the container.

        Returns:
            str: A string representing the name of the container.
        """

        endpoint = f"/fs-containers/{container_id}"
        payload = {}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "GET", self.base_url + endpoint, headers=headers, data=payload
        )

        return response.json()["data"]["name"]

    def showContainerContents(
        self, container_id: int, page: int = 0, per_page: int = 0
    ):
        """Shows the results of every file in the specified container.

        Parameters:
            container_id (int): The ID of the container.
            page (int): The current page
            per_page (int): Indicates how many files should be returned per page.
            with_result (bool): Whether the results of the files should be shown.

        Remarks:
            Pagination will be disabled when page and per_page are 0.

        Returns:

            Dictionary containing the following keys:
            songs (List<Song>): A list of songs that are in the container.
            current_page (int): The current page.
            last_page (int): The last page.
            per_page (int): The results shown per page.
        """

        if page == 0 or per_page == 0:  # do not use pagination here

            endpoint = f"/fs-containers/{container_id}/files/"

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
                arr = response.json()["data"]
                songs = []
                for file in arr:
                    song = self.showFileResults(container_id, file["id"])
                    songs.append(song.toJSON())
                    # state:
                    # 0 is processing, 1 is ready, -1 is no result, -2 is error

                return {
                    "songs": songs,
                    "current_page": 1,
                    "last_page": 1,
                    "per_page": 999,
                }
            else:
                raise ValueError(f"API returned the following error:\n{response.text}")
        else:  # use pagination here

            endpoint = (
                f"/fs-containers/{container_id}/files?page={page}&per_page={per_page}"
            )

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
                songs = []
                for file in data["data"]:
                    song = self.showFileResults(container_id, file["id"])
                    songs.append(song.toJSON())
                    # state:
                    # 0 is processing, 1 is ready, -1 is no result, -2 is error

                return {
                    "songs": songs,
                    "current_page": data["meta"]["current_page"],
                    "last_page": data["meta"]["last_page"],
                    "per_page": data["meta"]["per_page"],
                }
            else:
                raise ValueError(f"API returned the following error:\n{response.text}")

    def getFileDuration(self, container_id, file_id):
        """Gets the duration of an uploaded track.

        Returns:
            int: The length of the track in seconds.
        """

        endpoint = f"/fs-containers/{container_id}/files/{file_id}"

        payload = {}
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        response = requests.request(
            "GET", self.base_url + endpoint, headers=headers, data=payload
        )

        if response.status_code != 200:
            raise ValueError(f"API returned the following error:\n{response.text}")

        return response.json()["data"][0]["duration"]

    def showFileResults(self, container_id, file_id):
        """Show all the tracks that have been recognized in the file.

        Returns:
            List<Result>: A list of Result-objects that could be found.
        """
        endpoint = f"/fs-containers/{container_id}/files/{file_id}"

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
                        applemusic_link = ""
                        deezer_link = ""
                        youtube_link = ""

                        try:
                            isrc = found_music["result"]["external_ids"]["isrc"]
                        except (KeyError, TypeError):
                            isrc = ""

                        try:
                            spotify_link = f"{self.spotify_base_url}{found_music['result']['external_metadata']['spotify']['track']['id']}"
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

    def deleteFile(self, file_id, container_id):
        """Deletes a file in a container.

        Parameters:
            file_id (str): The ID of the file.
            container_id (str): The ID of the container.

        Returns:
            Bool: True if successful, false else.
        """

        endpoint = f"/fs-containers/{container_id}/files/{file_id}"

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

    def rescanFile(self, file_id, container_id):
        """Rescans a file to find tracks again.

        Parameters:
            file_id (str): The ID of the file.
            container_id (str): The ID of the container.

        Returns:
            Dict: The response as a dictionary.
        """

        endpoint = f"/fs-containers/{container_id}/files/{file_id}/rescan"

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
                        applemusic_link = ""
                        deezer_link = ""
                        youtube_link = ""

                        try:
                            isrc = found_music["result"]["external_ids"]["isrc"]
                        except (KeyError, TypeError):
                            isrc = ""

                        try:
                            spotify_link = f"{self.spotify_base_url}{found_music['result']['external_metadata']['spotify']['track']['id']}"
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
    )
