import string
import secrets
import base64
import requests

from app.settings import get_settings

settings = get_settings()


def get_random_string(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for i in range(length))


# refreshing an access token
def get_spotify_refresh_token():
    auth_string = settings.spotify_client_id + ":" + settings.spotify_client_secret
    auth_bytes = auth_string.encode("utf-8")
    auth_base64 = str(base64.b64encode(auth_bytes), "utf-8")

    url = "https://accounts.spotify.com/api/token"
    headers = {
        "Authorization": "Basic " + auth_base64,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}
    result = requests.post(url, headers=headers, data=data)
    json_result = result.json()
    token = json_result["access_token"]
    return token


# getting an access token
def get_spotify_access_token():
    url = (
        f"https://accounts.spotify.com/api/token?grant_type=client_credentials&"
        + f"client_id={settings.spotify_client_id}&"
        + f"client_secret={settings.spotify_client_secret}"
    )
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        result = requests.post(url, headers=headers, timeout=10)
        json_result = result.json()
        token = json_result.get("access_token", None)
        return token
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return None

def contains(list, filter):
    for x in list:
        if filter(x):
            return True
    return False


def find_index(list, filter):
    for i, element in enumerate(list):
        if filter(element):
            return i
    return -1
