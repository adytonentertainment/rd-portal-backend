from .v1.api import get_acrcloud_api
import stripe
from app.database.session import get_session

api = get_acrcloud_api()


def remove_unused_containers():
    containers = api.getAllContainers()
    db = get_session()
    # assumption: customer in database match with customers in stripe
    # also the old model is used

    print(containers)
