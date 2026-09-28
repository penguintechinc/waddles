from penguin_dal import DAL

from WaddlebotLibs.matterbridge_classes import matterbridgePayload
from WaddlebotLibs.botClasses import prize

import logging
import os
import requests

from dataclasses import asdict

# Get the Matterbridge URL from the config file or environment variables
matterbridgePostURL = os.getenv("MATTERBRIDGE_URL")

# set the logging level
logging.basicConfig(level=logging.INFO)

# Class to initialize the helpers class
class matterbridge_helpers:
    # Constructor
    def __init__(self, db: DAL):
        self.db = db

    # Helper function to create a matterbridge payload for a given community_id by checking all the gateways connected to the community. Returns None if no gateways are connected to the community.
    def create_matterbridge_payloads(self, community_id: int, message: str) -> list:
        if not community_id:
            raise ValueError("community_id must be provided")
        if not message:
            raise ValueError("message must be provided")

        community = self.db(self.db.communities.id == community_id).select().first()
        if not community:
            raise ValueError(f"No community found with id {community_id}")
        
        # In the routing table, get the routing_gateway_ids for the given community id. If the routing_gateway_ids list is empty, return an error.
        routings = self.db(self.db.routing.community_id == community.id).select().first()

        if not routings:
            logging.error("No routings found for the current community.")
            return None
        
        # Get the channel_id and account from the routing_gateway_ids
        channel_ids = []
        accounts = []
        if len(routings.routing_gateway_ids) == 0:
            logging.error("No routing gateways found for the current community. Unable to send a message.")
            return None
        
        for routing_gateway_id in routings.routing_gateway_ids:
            channel_id = self.get_channel_id(routing_gateway_id)
            account = self.get_account(routing_gateway_id)
            if channel_id and account:
                channel_ids.append(channel_id)
                accounts.append(account)

        # Create a matterbridge payload for each channel_id and account
        payloads = []
        for channel_id, account in zip(channel_ids, accounts):
            payload = matterbridgePayload(username="WaddleDBM", gateway="discord", account=account, text=message)
            payloads.append(payload)

        # Return the payloads
        return payloads

    # A helper function to send a message to Matterbridge with a given matterbridge payload. Returns a success message if the message is sent successfully.
    def send_matterbridge_message(self, payload: matterbridgePayload) -> None:
        # Send the message to Matterbridge
        try:
            requests.post(matterbridgePostURL, json=asdict(payload))
            logging.info("Message sent to Matterbridge successfully.")
        except Exception as e:
            logging.error(f"Error sending message to Matterbridge: {e}")

    # Helper function to get a routing_gateway channel_id from a given routing_gateway_id. If it doesnt exist, return null.
    def get_channel_id(self, routing_gateway_id: int) -> str:
        routing_gateway = self.db(self.db.routing_gateways.id == routing_gateway_id).select().first()
        return None if not routing_gateway else routing_gateway.channel_id

    # Helper function to get the account as a combination of the protocol and the server name from a given routing_gateway_id. If it doesnt exist, return null.
    def get_account(self, routing_gateway_id: int) -> str:
        routing_gateway = self.db(self.db.routing_gateways.id == routing_gateway_id).select().first()
        if not routing_gateway:
            return None
        gateway_server = self.db(self.db.gateway_servers.id == routing_gateway.gateway_server).select().first()
        if not gateway_server:
            return None
        return f"{gateway_server.protocol}.{gateway_server.name}"
    
    # A function to announce the winner of a giveaway in the chat of every gateway that the community is connected to, via Matterbridge.
    def announce_winner(self, giveaway, winner):
        payloads = self.create_matterbridge_payloads(giveaway.community_id, f"Giveaway with guid {giveaway.guid} is closed. Winner is {winner.identity_name}.")
        for payload in payloads:
            self.send_matterbridge_message(payload)
    