"""
MLC (Mechanical Licensing Collective) API Client

This module provides a client for interacting with the MLC Public API
to retrieve musical work registrations, authors, and publishers by ISRC.

Documentation: https://public-api.themlc.com/public-api/
API Spec: https://public-api.themlc.com/api/doc
"""

import os
import requests
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class MLCClient:
    """Client for MLC Public API"""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_url: Optional[str] = None
    ):
        """
        Initialize MLC API client

        Args:
            username: MLC API username (defaults to MLC_USERNAME env var)
            password: MLC API password (defaults to MLC_PASSWORD env var)
            api_url: MLC API base URL (defaults to MLC_API_URL env var)
        """
        self.api_url = api_url or os.getenv('MLC_API_URL', 'https://public-api.themlc.com')
        self.username = username or os.getenv('MLC_USERNAME')
        self.password = password or os.getenv('MLC_PASSWORD')

        if not self.username or not self.password:
            raise ValueError("MLC_USERNAME and MLC_PASSWORD must be set in environment or passed to constructor")

        # Token management
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.id_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None

    def _get_headers(self, authenticated: bool = True) -> Dict[str, str]:
        """Get HTTP headers for API requests"""
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        if authenticated:
            if not self.id_token or self._is_token_expired():
                self.authenticate()
            # Note: MLC API requires idToken, not accessToken
            headers['Authorization'] = f'Bearer {self.id_token}'

        return headers

    def _is_token_expired(self) -> bool:
        """Check if the current access token is expired"""
        if not self.token_expires_at:
            return True
        # Add 60 second buffer before actual expiration
        return datetime.now() >= (self.token_expires_at - timedelta(seconds=60))

    def authenticate(self) -> Dict[str, Any]:
        """
        Authenticate with MLC API and obtain access token

        Returns:
            Dict containing accessToken, refreshToken, and idToken

        Raises:
            requests.exceptions.RequestException: If authentication fails
        """
        url = f"{self.api_url}/oauth/token"

        payload = {
            "username": self.username,
            "password": self.password
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Content-Type': 'application/json'}
            )
            response.raise_for_status()

            data = response.json()

            self.access_token = data.get('accessToken')
            self.refresh_token = data.get('refreshToken')
            self.id_token = data.get('idToken')

            # Tokens typically expire in 1 hour (3600 seconds)
            # Adjust based on actual token expiration if provided in response
            expires_in = int(data.get('expiresIn', 3600))
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

            logger.info(f"Successfully authenticated with MLC API")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"MLC authentication failed: {str(e)}")
            raise

    def search_recordings_by_isrc(self, isrc: str) -> Dict[str, Any]:
        """
        Search for recordings by ISRC code

        Args:
            isrc: International Standard Recording Code (e.g., "USRC12345678")

        Returns:
            Dict containing recording information including:
            - artist: Artist name
            - title: Song title
            - isrc: ISRC code
            - recordLabel: Record label
            - mlcSongCode: Link to the associated musical work

        Example:
            >>> client = MLCClient()
            >>> result = client.search_recordings_by_isrc("USRC12345678")
            >>> print(result)
        """
        url = f"{self.api_url}/search/recordings"

        payload = {
            "isrc": isrc
        }

        try:
            # Note: Despite docs saying otherwise, this endpoint requires authentication
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(authenticated=True)
            )
            response.raise_for_status()

            # Handle 204 No Content - ISRC not found in database
            if response.status_code == 204 or not response.text:
                logger.info(f"No recording found for ISRC: {isrc}")
                return []

            data = response.json()
            logger.info(f"Found recording data for ISRC: {isrc}")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to search recordings for ISRC {isrc}: {str(e)}")
            raise

    def search_recordings(
        self,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        isrc: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Search for recordings using artist name, song title, or ISRC

        Args:
            artist: Artist name (optional)
            title: Song title (optional)
            isrc: ISRC code (optional)

        Returns:
            Dict containing list of matching recordings

        Note:
            At least one parameter must be provided
        """
        if not any([artist, title, isrc]):
            raise ValueError("At least one search parameter (artist, title, or isrc) must be provided")

        url = f"{self.api_url}/search/recordings"

        payload = {}
        if artist:
            payload['artist'] = artist
        if title:
            payload['title'] = title
        if isrc:
            payload['isrc'] = isrc

        try:
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(authenticated=True)
            )
            response.raise_for_status()

            # Handle 204 No Content - no results found
            if response.status_code == 204 or not response.text:
                logger.info(f"No recordings found for search criteria: {payload}")
                return []

            data = response.json()
            logger.info(f"Found recordings matching search criteria")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to search recordings: {str(e)}")
            raise

    def get_work_by_id(self, work_id: str) -> Dict[str, Any]:
        """
        Retrieve musical work details by MLC work ID

        Args:
            work_id: MLC work identifier

        Returns:
            Dict containing musical work information including:
            - primaryTitle: Main title of the work
            - akas: Alternate names
            - iswc: International Standard Work Code
            - mlcSongCode: MLC song identifier
            - writers: List of writers with IPI numbers and roles
            - publishers: List of publishers with share percentages
            - artists: Associated artists

        Requires:
            Authentication (Bearer token)
        """
        url = f"{self.api_url}/work/id/{work_id}"

        try:
            response = requests.get(
                url,
                headers=self._get_headers(authenticated=True)
            )
            response.raise_for_status()

            data = response.json()
            logger.info(f"Retrieved work data for ID: {work_id}")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get work {work_id}: {str(e)}")
            raise

    def search_works_by_songcode(
        self,
        song_codes: List[str]
    ) -> Dict[str, Any]:
        """
        Batch query multiple musical works by MLC song code

        Args:
            song_codes: List of MLC song codes

        Returns:
            Dict containing list of musical works with full details

        Requires:
            Authentication (Bearer token)
        """
        url = f"{self.api_url}/works"

        payload = {
            "songCodes": song_codes
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(authenticated=True)
            )
            response.raise_for_status()

            data = response.json()
            logger.info(f"Retrieved {len(song_codes)} works by song code")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get works by song code: {str(e)}")
            raise

    def search_by_title_and_writer(
        self,
        title: Optional[str] = None,
        writer_name: Optional[str] = None,
        writer_ipi: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Search for works by title and writer information

        Args:
            title: Song title (optional)
            writer_name: Writer's full name - will be split into first/last (optional)
            writer_ipi: Writer's IPI number (optional)

        Returns:
            Dict containing list of matching works

        Requires:
            Authentication (Bearer token)
        """
        url = f"{self.api_url}/search/songcode"

        payload = {}
        if title:
            payload['title'] = title

        # MLC API requires writers array with firstName/lastName structure
        if writer_name or writer_ipi:
            writer_obj = {}

            if writer_name:
                # Split name into first and last
                parts = writer_name.strip().split(None, 1)  # Split on first space
                if len(parts) == 2:
                    writer_obj['writerFirstName'] = parts[0]
                    writer_obj['writerLastName'] = parts[1]
                elif len(parts) == 1:
                    # If only one part, use it as last name
                    writer_obj['writerLastName'] = parts[0]

            if writer_ipi:
                writer_obj['writerIPI'] = writer_ipi

            if writer_obj:
                payload['writers'] = [writer_obj]

        if not payload:
            raise ValueError("At least one search parameter must be provided")

        try:
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(authenticated=True)
            )
            response.raise_for_status()

            if response.status_code == 204 or not response.text:
                logger.info(f"No works found for search criteria: {payload}")
                return []

            data = response.json()
            logger.info(f"Found works matching search criteria")
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to search works: {str(e)}")
            raise

    def get_complete_work_info_by_isrc(self, isrc: str) -> Dict[str, Any]:
        """
        Complete workflow: Search by ISRC and get full work details with writers/publishers

        This is a convenience method that:
        1. Searches for the recording by ISRC
        2. Extracts the MLC song code
        3. Retrieves the full work details including writers and publishers

        Args:
            isrc: International Standard Recording Code

        Returns:
            Dict containing:
            - recording: Recording information
            - work: Full musical work details with writers and publishers

        Example:
            >>> client = MLCClient()
            >>> info = client.get_complete_work_info_by_isrc("USRC12345678")
            >>> print(info['work']['writers'])
            >>> print(info['work']['publishers'])
        """
        # Step 1: Search for recording by ISRC
        recording_data = self.search_recordings_by_isrc(isrc)

        # Handle response format - API returns a list directly
        if not recording_data or (isinstance(recording_data, list) and len(recording_data) == 0):
            return {
                'recording': None,
                'work': None,
                'error': f'No recording found for ISRC: {isrc}'
            }

        # Get first recording (ISRC should be unique)
        if isinstance(recording_data, list):
            recording = recording_data[0]
        else:
            recording = recording_data

        mlc_song_code = recording.get('mlcsongCode') or recording.get('mlcSongCode')

        if not mlc_song_code:
            return {
                'recording': recording,
                'work': None,
                'error': 'No MLC song code found in recording data'
            }

        # Step 2: Get full work details using the song code
        try:
            work = self.get_work_by_id(mlc_song_code)

            return {
                'recording': recording,
                'work': work,
                'mlc_song_code': mlc_song_code
            }
        except Exception as e:
            logger.error(f"Failed to get work details for song code {mlc_song_code}: {str(e)}")
            return {
                'recording': recording,
                'work': None,
                'mlc_song_code': mlc_song_code,
                'error': str(e)
            }
