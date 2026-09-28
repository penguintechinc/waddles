"""
Validation Service Tests and Examples
======================================

Demonstrates usage of WorkflowValidationService with various scenarios.
This file shows how to validate workflows and use the validation results.

To run tests:
    python3 -m pytest tests/test_validation_service.py -v
"""

import logging
from datetime import datetime
from typing import Dict, Any

from models.workflow import (
    WorkflowDefinition, WorkflowMetadata, WorkflowConnection,
    WorkflowStatus
)
from models.nodes import (
    TriggerCommandConfig, ConditionIfConfig, ActionChatMessageConfig,
    FlowEndConfig, ConditionRule, OperatorType, PortDefinition,
    PortType, DataType, NodeType, DataTransformConfig,
    ActionWebhookConfig, HttpMethod, LoopForeachConfig,
    TriggerScheduleConfig
)
from services.validation_service import (
    WorkflowValidationService, ValidationResult
)


logger = logging.getLogger(__name__)


class TestWorkflowValidator:
    """Test cases for workflow validation"""

    @staticmethod
    def create_sample_trigger_node() -> TriggerCommandConfig:
        """Create a sample trigger command node"""
        return TriggerCommandConfig(
            node_id="trigger_1",
            label="Help Command",
            position={"x": 100, "y": 100},
            command_pattern="!help",
            platforms=["twitch", "discord"],
            output_ports=[
                PortDefinition(
                    name="success",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.OBJECT,
                    label="Success Output"
                )
            ]
        )

    @staticmethod
    def create_sample_condition_node() -> ConditionIfConfig:
        """Create a sample condition node"""
        return ConditionIfConfig(
            node_id="condition_1",
            label="Check User Level",
            position={"x": 300, "y": 100},
            condition=[
                ConditionRule(
                    variable="user.level",
                    operator=OperatorType.GREATER_EQUAL,
                    value=5
                )
            ],
            output_true_port="true",
            output_false_port="false",
            input_ports=[
                PortDefinition(
                    name="input",
                    port_type=PortType.INPUT,
                    data_type=DataType.OBJECT
                )
            ],
            output_ports=[
                PortDefinition(
                    name="true",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.OBJECT
                ),
                PortDefinition(
                    name="false",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.OBJECT
                )
            ]
        )

    @staticmethod
    def create_sample_action_node() -> ActionChatMessageConfig:
        """Create a sample action chat message node"""
        return ActionChatMessageConfig(
            node_id="action_1",
            label="Send Help Message",
            position={"x": 500, "y": 100},
            message_template="Here's the help: {help_text}",
            destination="current_channel",
            platform="twitch",
            input_ports=[
                PortDefinition(
                    name="input",
                    port_type=PortType.INPUT,
                    data_type=DataType.OBJECT
                )
            ],
            output_ports=[
                PortDefinition(
                    name="success",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.BOOLEAN
                )
            ]
        )

    @staticmethod
    def create_sample_end_node() -> FlowEndConfig:
        """Create a sample end node"""
        return FlowEndConfig(
            node_id="end_1",
            label="End Workflow",
            position={"x": 700, "y": 100},
            input_ports=[
                PortDefinition(
                    name="input",
                    port_type=PortType.INPUT,
                    data_type=DataType.OBJECT
                )
            ]
        )

    @staticmethod
    def test_valid_workflow():
        """Test validation of a valid workflow"""
        print("\n=== Test: Valid Workflow ===")

        # Create metadata
        metadata = WorkflowMetadata(
            workflow_id="workflow_test_1",
            name="Help Command Workflow",
            description="Responds to help commands",
            author_id="user_123",
            community_id="community_456",
            status=WorkflowStatus.DRAFT
        )

        # Create nodes
        trigger = TestWorkflowValidator.create_sample_trigger_node()
        condition = TestWorkflowValidator.create_sample_condition_node()
        action = TestWorkflowValidator.create_sample_action_node()
        end = TestWorkflowValidator.create_sample_end_node()

        # Create workflow
        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes={
                trigger.node_id: trigger,
                condition.node_id: condition,
                action.node_id: action,
                end.node_id: end,
            },
            connections=[
                WorkflowConnection(
                    connection_id="conn_1",
                    from_node_id="trigger_1",
                    from_port_name="success",
                    to_node_id="condition_1",
                    to_port_name="input"
                ),
                WorkflowConnection(
                    connection_id="conn_2",
                    from_node_id="condition_1",
                    from_port_name="true",
                    to_node_id="action_1",
                    to_port_name="input"
                ),
                WorkflowConnection(
                    connection_id="conn_3",
                    from_node_id="action_1",
                    from_port_name="success",
                    to_node_id="end_1",
                    to_port_name="input"
                ),
            ]
        )

        # Validate
        validator = WorkflowValidationService()
        result = validator.validate_workflow(workflow)

        print(f"Is Valid: {result.is_valid}")
        print(f"Errors: {result.errors}")
        print(f"Warnings: {result.warnings}")

        assert result.is_valid, "Valid workflow should pass validation"
        print("PASSED")

    @staticmethod
    def test_missing_trigger():
        """Test validation of workflow with no trigger nodes"""
        print("\n=== Test: Missing Trigger Node ===")

        metadata = WorkflowMetadata(
            workflow_id="workflow_test_2",
            name="No Trigger Workflow",
            description="Workflow without trigger",
            author_id="user_123",
            community_id="community_456"
        )

        # Only action and end nodes, no trigger
        action = TestWorkflowValidator.create_sample_action_node()
        end = TestWorkflowValidator.create_sample_end_node()

        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes={
                action.node_id: action,
                end.node_id: end,
            }
        )

        validator = WorkflowValidationService()
        result = validator.validate_workflow(workflow)

        print(f"Is Valid: {result.is_valid}")
        print(f"Warnings: {result.warnings}")

        assert not result.is_valid, "Workflow without trigger should fail"
        assert any("trigger" in w.lower() for w in result.warnings), \
            "Should warn about missing trigger"
        print("PASSED")

    @staticmethod
    def test_invalid_connection():
        """Test validation with invalid connection"""
        print("\n=== Test: Invalid Connection ===")

        metadata = WorkflowMetadata(
            workflow_id="workflow_test_3",
            name="Bad Connection Workflow",
            description="Workflow with invalid connection",
            author_id="user_123",
            community_id="community_456"
        )

        trigger = TestWorkflowValidator.create_sample_trigger_node()
        end = TestWorkflowValidator.create_sample_end_node()

        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes={
                trigger.node_id: trigger,
                end.node_id: end,
            },
            connections=[
                WorkflowConnection(
                    connection_id="bad_conn",
                    from_node_id="trigger_1",
                    from_port_name="nonexistent_port",  # Invalid port
                    to_node_id="end_1",
                    to_port_name="input"
                )
            ]
        )

        validator = WorkflowValidationService()
        result = validator.validate_workflow(workflow)

        print(f"Is Valid: {result.is_valid}")
        print(f"Errors: {result.errors}")

        assert not result.is_valid, "Invalid connection should fail validation"
        print("PASSED")

    @staticmethod
    def test_cycle_detection():
        """Test detection of cycles in workflow"""
        print("\n=== Test: Cycle Detection ===")

        metadata = WorkflowMetadata(
            workflow_id="workflow_test_4",
            name="Cyclic Workflow",
            description="Workflow with cycle",
            author_id="user_123",
            community_id="community_456"
        )

        node_a = TriggerCommandConfig(
            node_id="node_a",
            label="Node A",
            position={"x": 100, "y": 100},
            command_pattern="!test",
            platforms=["twitch"],
            output_ports=[
                PortDefinition(
                    name="out",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.OBJECT
                )
            ]
        )

        node_b = ActionChatMessageConfig(
            node_id="node_b",
            label="Node B",
            position={"x": 300, "y": 100},
            message_template="Test",
            input_ports=[
                PortDefinition(
                    name="in",
                    port_type=PortType.INPUT,
                    data_type=DataType.OBJECT
                )
            ],
            output_ports=[
                PortDefinition(
                    name="out",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.OBJECT
                )
            ]
        )

        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes={
                node_a.node_id: node_a,
                node_b.node_id: node_b,
            },
            connections=[
                WorkflowConnection(
                    connection_id="conn_a_b",
                    from_node_id="node_a",
                    from_port_name="out",
                    to_node_id="node_b",
                    to_port_name="in"
                ),
                WorkflowConnection(
                    connection_id="conn_b_a",
                    from_node_id="node_b",
                    from_port_name="out",
                    to_node_id="node_a",
                    to_port_name="input"  # This creates a cycle
                )
            ]
        )

        validator = WorkflowValidationService()
        result = validator.validate_workflow(workflow)

        print(f"Is Valid: {result.is_valid}")
        print(f"Errors: {result.errors}")

        assert not result.is_valid, "Cyclic workflow should fail validation"
        print("PASSED")

    @staticmethod
    def test_node_configuration_validation():
        """Test validation of node configuration"""
        print("\n=== Test: Node Configuration Validation ===")

        metadata = WorkflowMetadata(
            workflow_id="workflow_test_5",
            name="Bad Config Workflow",
            description="Workflow with invalid node config",
            author_id="user_123",
            community_id="community_456"
        )

        # Action with invalid URL
        bad_action = ActionWebhookConfig(
            node_id="bad_action",
            label="Bad Webhook",
            position={"x": 100, "y": 100},
            url="not a valid url",  # Invalid URL
            method=HttpMethod.POST,
            timeout_seconds=10
        )

        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes={
                bad_action.node_id: bad_action,
            }
        )

        validator = WorkflowValidationService()
        result = validator.validate_workflow(workflow)

        print(f"Is Valid: {result.is_valid}")
        print(f"Node Errors: {result.node_validation_errors}")

        assert not result.is_valid, "Invalid node config should fail validation"
        assert "bad_action" in result.node_validation_errors, \
            "Should have errors for bad_action node"
        print("PASSED")

    @staticmethod
    def test_complexity_limits():
        """Test workflow complexity limits"""
        print("\n=== Test: Complexity Limits ===")

        metadata = WorkflowMetadata(
            workflow_id="workflow_test_6",
            name="Large Workflow",
            description="Workflow with many nodes",
            author_id="user_123",
            community_id="community_456"
        )

        # Create too many nodes
        nodes = {}
        connections = []

        trigger = TestWorkflowValidator.create_sample_trigger_node()
        nodes[trigger.node_id] = trigger
        prev_node_id = trigger.node_id

        # Create 101 nodes (exceeds MAX_NODES = 100)
        for i in range(101):
            node = ActionChatMessageConfig(
                node_id=f"action_{i}",
                label=f"Action {i}",
                position={"x": 100 + i * 10, "y": 100},
                message_template=f"Message {i}",
                input_ports=[
                    PortDefinition(
                        name="in",
                        port_type=PortType.INPUT,
                        data_type=DataType.OBJECT
                    )
                ],
                output_ports=[
                    PortDefinition(
                        name="out",
                        port_type=PortType.OUTPUT,
                        data_type=DataType.OBJECT
                    )
                ]
            )
            nodes[node.node_id] = node

        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes=nodes
        )

        validator = WorkflowValidationService()
        result = validator.validate_workflow(workflow)

        print(f"Is Valid: {result.is_valid}")
        print(f"Errors: {result.errors}")

        assert not result.is_valid, "Workflow exceeding max nodes should fail"
        print("PASSED")

    @staticmethod
    def test_security_validation():
        """Test security validation for malicious patterns"""
        print("\n=== Test: Security Validation ===")

        metadata = WorkflowMetadata(
            workflow_id="workflow_test_7",
            name="Malicious Workflow",
            description="Workflow with security issues",
            author_id="user_123",
            community_id="community_456"
        )

        trigger = TestWorkflowValidator.create_sample_trigger_node()

        # Action with malicious code pattern
        bad_action = DataTransformConfig(
            node_id="bad_transform",
            label="Bad Transform",
            position={"x": 300, "y": 100},
            transformations={
                "result": "eval('malicious code')"  # Malicious pattern
            },
            expression_language="python"
        )

        workflow = WorkflowDefinition(
            metadata=metadata,
            nodes={
                trigger.node_id: trigger,
                bad_action.node_id: bad_action,
            }
        )

        validator = WorkflowValidationService()
        result = validator.validate_workflow(workflow)

        print(f"Is Valid: {result.is_valid}")
        print(f"Node Errors: {result.node_validation_errors}")

        assert not result.is_valid, "Malicious code should fail validation"
        assert "bad_transform" in result.node_validation_errors, \
            "Should detect malicious pattern in node"
        print("PASSED")

    @staticmethod
    def test_cron_validation():
        """Test cron expression validation"""
        print("\n=== Test: Cron Expression Validation ===")

        metadata = WorkflowMetadata(
            workflow_id="workflow_test_8",
            name="Schedule Workflow",
            description="Workflow with schedule",
            author_id="user_123",
            community_id="community_456"
        )

        # Valid cron
        valid_schedule = TriggerScheduleConfig(
            node_id="schedule_1",
            label="Every 6 Hours",
            position={"x": 100, "y": 100},
            cron_expression="0 */6 * * *",
            timezone="UTC",
            output_ports=[
                PortDefinition(
                    name="out",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.OBJECT
                )
            ]
        )

        # Invalid cron
        invalid_schedule = TriggerScheduleConfig(
            node_id="schedule_2",
            label="Bad Cron",
            position={"x": 300, "y": 100},
            cron_expression="invalid cron expression",
            timezone="UTC",
            output_ports=[
                PortDefinition(
                    name="out",
                    port_type=PortType.OUTPUT,
                    data_type=DataType.OBJECT
                )
            ]
        )

        end = TestWorkflowValidator.create_sample_end_node()

        # Test valid cron
        workflow1 = WorkflowDefinition(
            metadata=metadata,
            nodes={
                valid_schedule.node_id: valid_schedule,
                end.node_id: end,
            }
        )

        validator = WorkflowValidationService()
        result1 = validator.validate_workflow(workflow1)
        print(f"Valid Cron - Is Valid: {result1.is_valid}")

        # Test invalid cron
        workflow2 = WorkflowDefinition(
            metadata=metadata,
            nodes={
                invalid_schedule.node_id: invalid_schedule,
                end.node_id: end,
            }
        )

        result2 = validator.validate_workflow(workflow2)
        print(f"Invalid Cron - Is Valid: {result2.is_valid}")
        print(f"Invalid Cron - Errors: {result2.node_validation_errors}")

        assert result1.is_valid, "Valid cron should pass"
        assert not result2.is_valid, "Invalid cron should fail"
        print("PASSED")

    @staticmethod
    def run_all_tests():
        """Run all test cases"""
        print("\n" + "=" * 60)
        print("WORKFLOW VALIDATION SERVICE TEST SUITE")
        print("=" * 60)

        try:
            TestWorkflowValidator.test_valid_workflow()
            TestWorkflowValidator.test_missing_trigger()
            TestWorkflowValidator.test_invalid_connection()
            TestWorkflowValidator.test_cycle_detection()
            TestWorkflowValidator.test_node_configuration_validation()
            TestWorkflowValidator.test_complexity_limits()
            TestWorkflowValidator.test_security_validation()
            TestWorkflowValidator.test_cron_validation()

            print("\n" + "=" * 60)
            print("ALL TESTS PASSED!")
            print("=" * 60 + "\n")

        except AssertionError as e:
            print(f"\n!!! TEST FAILED: {e}\n")
            raise


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    TestWorkflowValidator.run_all_tests()
