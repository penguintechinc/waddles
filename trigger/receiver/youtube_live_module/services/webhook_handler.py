"""
Webhook Handler Service
=======================

Handles PubSubHubbub (WebSub) callbacks for YouTube channel notifications.
Processes stream start/end events from YouTube's Atom feed updates.

Security (XXE): the callback endpoint is a public, unauthenticated HTTP POST
target, so the Atom body is attacker-controlled.  It is parsed ONLY through
``defusedxml`` with DTDs, entity declarations and external references
forbidden (blocks XXE file-read/SSRF and billion-laughs entity expansion).
Never use the stdlib ``xml.etree.ElementTree`` parse functions here;
``tests/unit/test_no_unsafe_xml.py`` enforces this.
"""

import logging
from typing import Dict, Optional

# Stdlib ElementTree is imported ONLY for the Element type annotation.
# Parsing goes through defusedxml (SafeET) — see module docstring.
from xml.etree.ElementTree import Element  # nosec B405

import httpx
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from config import Config

logger = logging.getLogger(__name__)


# XML Namespaces for YouTube Atom feeds
NAMESPACES = {
    'atom': 'http://www.w3.org/2005/Atom',
    'yt': 'http://www.youtube.com/xml/schemas/2015',
    'media': 'http://search.yahoo.com/mrss/',
}


class WebhookHandler:
    """
    Handles PubSubHubbub webhook callbacks from YouTube.

    Processes Atom feed updates to detect new videos and live streams,
    forwarding events to the router module.
    """

    def __init__(self):
        """Initialize the webhook handler."""
        self._http_client: Optional[httpx.AsyncClient] = None

    async def start(self):
        """Initialize HTTP client."""
        self._http_client = httpx.AsyncClient(timeout=30.0)

    async def stop(self):
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()

    def verify_subscription(
        self,
        hub_mode: str,
        hub_topic: str,
        hub_challenge: str,
        hub_lease_seconds: Optional[str] = None
    ) -> Optional[str]:
        """
        Verify a PubSubHubbub subscription request.

        Args:
            hub_mode: 'subscribe' or 'unsubscribe'
            hub_topic: The topic URL being subscribed to
            hub_challenge: Challenge string to echo back
            hub_lease_seconds: Lease duration (for subscribe)

        Returns:
            The hub_challenge to echo back, or None if invalid
        """
        if hub_mode not in ('subscribe', 'unsubscribe'):
            logger.warning(f"Invalid hub.mode: {hub_mode}")
            return None

        # Verify this is a YouTube channel feed URL
        if 'youtube.com/xml/feeds/videos.xml' not in hub_topic:
            logger.warning(f"Invalid topic URL: {hub_topic}")
            return None

        # Extract channel ID from topic
        channel_id = self._extract_channel_id(hub_topic)
        if not channel_id:
            logger.warning(f"Could not extract channel ID from: {hub_topic}")
            return None

        logger.info(
            f"Verified {hub_mode} for channel {channel_id}, "
            f"lease: {hub_lease_seconds}s"
        )

        return hub_challenge

    async def process_notification(self, body: bytes) -> Dict:
        """
        Process a PubSubHubbub notification.

        Args:
            body: Raw XML body of the notification

        Returns:
            Dict with parsed event data
        """
        try:
            root = SafeET.fromstring(
                body, forbid_dtd=True, forbid_entities=True, forbid_external=True
            )

            # Get channel info from feed
            channel_id = None
            channel_name = None

            author = root.find('atom:author', NAMESPACES)
            if author is not None:
                uri = author.find('atom:uri', NAMESPACES)
                if uri is not None and uri.text:
                    # Extract channel ID from URI
                    channel_id = uri.text.split('/')[-1]
                name = author.find('atom:name', NAMESPACES)
                if name is not None:
                    channel_name = name.text

            # Process each entry (video)
            events = []
            for entry in root.findall('atom:entry', NAMESPACES):
                event = self._parse_entry(entry, channel_id, channel_name)
                if event:
                    events.append(event)
                    await self._forward_event(event)

            return {
                'success': True,
                'channel_id': channel_id,
                'events_processed': len(events)
            }

        except SafeET.ParseError as e:
            logger.error(f"Failed to parse notification XML: {e}")
            return {'success': False, 'error': 'Invalid XML'}
        except DefusedXmlException as e:
            # Hostile payload (DTD / entity declaration / external reference).
            # Log the class only: the message embeds attacker-chosen names.
            logger.error(
                f"Rejected unsafe XML in notification: {type(e).__name__}"
            )
            return {'success': False, 'error': 'Invalid XML'}
        except Exception as e:
            logger.error(f"Error processing notification: {e}")
            return {'success': False, 'error': str(e)}

    def _extract_channel_id(self, topic_url: str) -> Optional[str]:
        """Extract channel ID from a topic URL."""
        try:
            if 'channel_id=' in topic_url:
                return topic_url.split('channel_id=')[1].split('&')[0]
        except Exception:
            pass
        return None

    def _parse_entry(
        self,
        entry: Element,
        channel_id: Optional[str],
        channel_name: Optional[str]
    ) -> Optional[Dict]:
        """
        Parse a feed entry into an event.

        Args:
            entry: XML entry element
            channel_id: Channel ID from feed
            channel_name: Channel name from feed

        Returns:
            Event dict or None
        """
        try:
            # Get video ID
            video_id_elem = entry.find('yt:videoId', NAMESPACES)
            if video_id_elem is None:
                return None
            video_id = video_id_elem.text

            # Get video title
            title_elem = entry.find('atom:title', NAMESPACES)
            title = title_elem.text if title_elem is not None else ''

            # Get published/updated time
            published_elem = entry.find('atom:published', NAMESPACES)
            published = published_elem.text if published_elem is not None else ''

            updated_elem = entry.find('atom:updated', NAMESPACES)
            updated = updated_elem.text if updated_elem is not None else ''

            # Get video link
            link_elem = entry.find("atom:link[@rel='alternate']", NAMESPACES)
            video_url = link_elem.get('href') if link_elem is not None else ''

            # Determine event type
            # New uploads trigger as 'videoPublished'
            # We'll check if it's a live stream via the API later
            event_type = 'videoPublished'

            return {
                'type': event_type,
                'platform': 'youtube',
                'channel_id': channel_id,
                'channel_name': channel_name,
                'video_id': video_id,
                'title': title,
                'url': video_url,
                'published_at': published,
                'updated_at': updated
            }

        except Exception as e:
            logger.error(f"Failed to parse entry: {e}")
            return None

    async def _forward_event(self, event: Dict):
        """Forward an event to the router module."""
        if not self._http_client:
            return

        try:
            response = await self._http_client.post(
                f"{Config.ROUTER_API_URL}/events",
                json=event,
                timeout=10.0
            )

            if response.status_code != 200:
                logger.warning(
                    f"Router returned {response.status_code} for webhook event"
                )
            else:
                logger.info(
                    f"Forwarded {event['type']} event for video "
                    f"{event.get('video_id')}"
                )

        except Exception as e:
            logger.error(f"Failed to forward event to router: {e}")
