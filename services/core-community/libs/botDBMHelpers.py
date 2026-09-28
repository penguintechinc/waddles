# Imports
from functools import lru_cache
from penguin_dal import DAL, Field
from urllib.parse import unquote

from json import load as jload
from json import dump as jdump

from requests import request

# Import the necessary classes from the botclasses module
from WaddlebotLibs.botClasses import role, community, alias_command, module, identity, routing_gateway, text_response

# Class to initialize the helpers class
class dbm_helpers:
    # Constructor
    def __init__(self, db: DAL):
        self.db = db

    # Get the owner role for a given community_id.
    # @lru_cache(maxsize=128)
    def get_owner_role(self, community_id: int) -> role:
        
        return (
            self.db(
                (self.db.roles.community_id == community_id)
                & (self.db.roles.name == "Owner")
            )
            .select()
            .first()
        )

    # Function to decode names with space in
    def decode_name(self, name: str) -> str:
        if not name:
            return None
        name = name.replace("%20", " ")
        name = name.replace("_", " ")

        return unquote(name)
    
    #Function to replace the first character of a string with a hash if it is an underscore.
    def replace_first_char(self, name: str) -> str:
        if name[0] == "_":
            name = "#" + name[1:]
        return name
    
    # Helper function to retrieve a community object from a given name
    def get_community(self, name: str) -> community:
        return self.db(self.db.communities.community_name == name).select().first()
    
    def get_identity(self, identity_name: str) -> identity:
        return self.db(self.db.identities.name == identity_name).select().first()
    
    # Helper function to return a text response object from a given text value and a community id
    def get_text_response(self, text: str, community_id: int) -> text_response:    
        return self.db((self.db.text_responses.community_id == community_id) & (self.db.text_responses.text_val == text)).select().first()

    # Helper function to return a alias command object from a given alias value and a community id
    def get_alias_command(self, alias: str, community_id: int) -> alias_command:    
        return self.db((self.db.alias_commands.community_id == community_id) & (self.db.alias_commands.alias_val == alias)).select().first()

    # Helper function to raplace all spaces in given command string with _ and return the new string
    def replace_spaces(self, command: str) -> str:
        return command.replace(" ", "_")

    # Helper function to check if a given command exists in the modules table
    def command_exists(self, command: str) -> bool:
        command = self.replace_spaces(command)
        # return self.db(self.db.modules.metadata.like(f'%"{command}"%')).select().first()

        return self.db(self.db.modules.metadata.like(f'%"{command}"%')).count() > 0
    
    

    # A helper function that checks if a given identity exists in a given community. Returns True if the identity exists in the community, False otherwise.
    def identity_in_community(self, identity_name: str, community_name: str) -> bool:
        identity = self.db(self.db.identities.name == identity_name).select(self.db.identities.id).first()
        community = self.db(self.db.communities.community_name == community_name).select(self.db.communities.id).first()
        
        if not identity or not community:
            return False
        
        membership = self.db((self.db.community_members.identity_id == identity.id) &
                        (self.db.community_members.community_id == community.id)).select().first()
        
        return bool(membership)

    # A helper function that checks if a given identity is an admin of a given community. Returns True if the identity is an admin, False otherwise.
    def identity_is_admin(self, identity_name: str, community_name: str) -> bool:
        identity = self.db(self.db.identities.name == identity_name).select(self.db.identities.id).first()
        community = self.db(self.db.communities.community_name == community_name).select(self.db.communities.id).first()
        
        if not identity or not community:
            return False
        
        membership = self.db((self.db.community_members.identity_id == identity.id) &
                        (self.db.community_members.community_id == community.id)).select().first()
        if not membership:
            return False
        role = self.db(self.db.roles.id == membership.role_id).select().first()
        return role.name in ['Admin', 'Owner', 'admin', 'owner']
    
    # A helper function that sets the role of a given identity name in a given community, to a given role name.
    # The table that needs to be updated is the community_members table. Returns a dictionary with a message.
    def set_role(self, receiver: identity, role: role, community: community) -> dict:
        if not receiver or not role or not community:
            return dict(msg="Please provide a receiver, role and community.")
        
        # Get the membership record of the receiver in the community.
        membership = self.db(
            (self.db.community_members.identity_id == receiver.id) &
            (self.db.community_members.community_id == community.id)
        ).select().first()

        if not membership:
            return dict(msg="Receiver is not a member of the community.")
        
        # Update the role of the receiver in the community.
        membership.update_record(role_id=role.id)
        return dict(msg="Role updated.")

    def is_community_module(self, module: module) -> bool:
        module_type = self.db(self.db.module_types.id == module.module_type_id).select().first()
        return module_type.name == "Community"
    
    
    # Helper function to get a community record by its name.
    def get_community_record_by_name(self, name: str) -> community:
        return self.db(self.db.communities.community_name == name).select().first()

    # Helper function to get an identity record by its name.
    def get_identity_record_by_name(self, name: str) -> identity:
        return self.db(self.db.identities.name == name).select().first()

    # Help function to get the "Member" role id for a given community.
    def get_member_role_id(self, community_id: int) -> int:
        return self.db(
            (self.db.roles.community_id == community_id) & (self.db.roles.name == "Member")
        ).select(self.db.roles.id).first().id

    # A helper function that uses the community and an identity to retrieve the identity's role in the community.
    def get_identity_role_in_community(self, identity_name: str, community_name: str) -> role:
        identity = self.db(self.db.identities.name == identity_name).select().first()
        community = self.db(self.db.communities.community_name == community_name).select().first()
        
        if not identity or not community:
            return None
        
        membership = self.db((self.db.community_members.identity_id == identity.id) &
                        (self.db.community_members.community_id == community.id)).select().first()
        
        if not membership:
            return None
        
        role = self.db(self.db.roles.id == membership.role_id).select().first()
        return role

    # Helper function to set the role of all identities in a community to the default "Member" role when a role is deleted.
    # Only identities with the deleted role are affected.
    def set_default_role_for_identities_in_community(self, community_id: int, role_id: int):
        DEFAULT_ROLE_ID = self.get_member_role_id(community_id)

        self.db(
            (self.db.community_members.community_id == community_id) &
            (self.db.community_members.role_id == role_id)
        ).update(role_id=DEFAULT_ROLE_ID)
        identities = self.db(self.db.community_members.community_id == community_id).select()

        if identities:
            for identity in identities:
                if identity.role_id == role_id:
                    identity.update_record(role_id=1)

    # db.py controller helper functions
    # Function to insert a given table_name and columns into a configuration file.
    def insert_table_into_config(self, table_name: str, columns: dict):
        # Check if the table_name and columns are given.
        if not table_name or not columns:
            return dict(msg="Please provide a table_name and columns.")

        filePath = "applications/WaddleDBM/models/external_tables.json"
        tableList = []

        # Check if the configuration file exists.
        try:
            with open(filePath, "r") as file:
                tableList = jload(file)

        except FileNotFoundError:
            print("Configuration file not found. Creating a new configuration file.")
        
        # Check if the table_name already exists in the configuration file.
        if len(tableList) > 0:
            for table in tableList:
                if "table_name" in table and table["table_name"] == table_name:
                    return dict(msg=f"Table {table_name} already exists in the configuration file.")
        
        # Insert the table_name and columns into the configuration file.
        config = {
            "table_name": table_name,
            "columns": columns
        }

        tableList.append(config)
        
        # Write the updated configuration file.
        with open(filePath, "w") as file:
            jdump(tableList, file)
        
        return dict(msg=f"Table {table_name} inserted into the configuration file.")

    # Function to get all tables from the configuration file, and define them in the penguin-dal database.
    def define_tables_from_config(self):
        print("Defining tables from the configuration file.")

        filePath = "applications/WaddleDBM/models/external_tables.json"
        tableList = []

        # Check if the configuration file exists.
        try:
            with open(filePath, "r") as file:
                tableList = jload(file)

        except FileNotFoundError:
            return dict(msg="Configuration file not found.")
        
        # Check if the tableList is empty.
        if not tableList:
            return dict(msg="No tables found in the configuration file.")
        
        # Define all the tables in the configuration file.
        if len(tableList) > 0:
            print("Found tables in the configuration file. Defining tables in the database.")
            for table in tableList:
                table_name = table.get("table_name")
                columns = table.get("columns")
                
                # Check if the table_name and columns are given.
                if not table_name or not columns:
                    return dict(msg="Please provide a table_name and columns.")
                
                # Check if the table already exists.
                if self.db.get(table_name):
                    return dict(msg=f"Table {table_name} already exists.")
                
                # The columns is read as a dictionary with the column name as the key and the column type as the value.
                # Convert the dictionary into 2 lists, one for the column names and one for the column types.
                tColumns = list(columns.keys())
                tTypes = list(columns.values())
                
                try:
                    # Create the table with the given columns.
                    self.db.define_table(table_name, *[Field(column_name, column_type) for column_name, column_type in zip(tColumns, tTypes)])

                    # Commit the table creation.
                    self.db.commit()

                    print(f"Table {table_name} defined with columns {tColumns}.")
                except Exception as e:
                    return dict(msg=f"Error creating table {table_name}. Error: {e}")
        
        # Print all the tables defined from the configuration file.
        print(self.db.tables())

        return dict(msg="All tables defined from the configuration file.")
    
    # Helper function to return a routing_gateway by a given channel_id and account. If it doesnt exist, return null.
    def get_routing_gateway(self, channel_id: str, account: str) -> routing_gateway:
        # First, split the account into the protocol and the server name by splitting the account string by the first dot.
        account_split = account.split(".", 1)
        if len(account_split) != 2:
            return None
        
        protocol = account_split[0]
        server_name = account_split[1]

        gateway_server = self.db((self.db.gateway_servers.protocol == protocol) & (self.db.gateway_servers.name == server_name)).select().first()

        if not gateway_server:
            return None
        
        routing_gateway = self.db((self.db.routing_gateways.channel_id == channel_id) & (self.db.routing_gateways.gateway_server == gateway_server.id)).select().first()

        return routing_gateway