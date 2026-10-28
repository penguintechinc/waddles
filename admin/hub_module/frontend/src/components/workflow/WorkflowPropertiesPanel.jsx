import { useState, useEffect } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';

function WorkflowPropertiesPanel({ selectedNode, onUpdateNode, onClose }) {
  const [formData, setFormData] = useState({});

  useEffect(() => {
    if (selectedNode) {
      setFormData(selectedNode.data.config || {});
    }
  }, [selectedNode]);

  if (!selectedNode) {
    return (
      <div className="w-80 bg-navy-900 border-l border-navy-700 p-6 flex flex-col items-center justify-center">
        <div className="text-center">
          <div className="w-16 h-16 rounded-full bg-navy-800 flex items-center justify-center mx-auto mb-4">
            <span className="text-3xl">📋</span>
          </div>
          <h3 className="text-sm font-semibold text-white mb-2">No Node Selected</h3>
          <p className="text-xs text-navy-400">
            Select a node to configure its properties
          </p>
        </div>
      </div>
    );
  }

  const handleChange = (field, value) => {
    setFormData((prev) => ({
      ...prev,
      [field]: value,
    }));
  };

  const handleSave = () => {
    onUpdateNode(selectedNode.id, {
      ...selectedNode.data,
      config: formData,
    });
  };

  const renderTriggerFields = () => (
    <>
      <div>
        <label htmlFor="workflow-properties-panel-trigger-type" className="block text-xs font-medium text-navy-300 mb-1">
          Trigger Type
        </label>
        <select
          id="workflow-properties-panel-trigger-type"
          value={formData.triggerType || ''}
          onChange={(e) => handleChange('triggerType', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">Select type...</option>
          <option value="chat_message">Chat Message</option>
          <option value="command">Command</option>
          <option value="event">Platform Event</option>
          <option value="webhook">Webhook</option>
          <option value="schedule">Schedule</option>
        </select>
      </div>

      <div>
        <label htmlFor="workflow-properties-panel-platform" className="block text-xs font-medium text-navy-300 mb-1">
          Platform
        </label>
        <select
          id="workflow-properties-panel-platform"
          value={formData.platform || ''}
          onChange={(e) => handleChange('platform', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">All platforms</option>
          <option value="twitch">Twitch</option>
          <option value="discord">Discord</option>
          <option value="slack">Slack</option>
        </select>
      </div>

      {formData.triggerType === 'command' && (
        <div>
          <label htmlFor="workflow-properties-panel-command" className="block text-xs font-medium text-navy-300 mb-1">
            Command
          </label>
          <input
            id="workflow-properties-panel-command"
            type="text"
            value={formData.command || ''}
            onChange={(e) => handleChange('command', e.target.value)}
            placeholder="!mycommand"
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500"
          />
        </div>
      )}

      {formData.triggerType === 'schedule' && (
        <div>
          <label htmlFor="workflow-properties-panel-cron-expression" className="block text-xs font-medium text-navy-300 mb-1">
            Cron Expression
          </label>
          <input
            id="workflow-properties-panel-cron-expression"
            type="text"
            value={formData.cronExpression || ''}
            onChange={(e) => handleChange('cronExpression', e.target.value)}
            placeholder="0 0 * * *"
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500"
          />
        </div>
      )}
    </>
  );

  const renderConditionFields = () => (
    <>
      <div>
        <label htmlFor="workflow-properties-panel-condition-type" className="block text-xs font-medium text-navy-300 mb-1">
          Condition Type
        </label>
        <select
          id="workflow-properties-panel-condition-type"
          value={formData.conditionType || ''}
          onChange={(e) => handleChange('conditionType', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">Select type...</option>
          <option value="if_else">If/Else</option>
          <option value="user_role">User Role</option>
          <option value="permission">Permission</option>
          <option value="text_match">Text Match</option>
          <option value="compare">Compare Values</option>
        </select>
      </div>

      <div>
        <label htmlFor="workflow-properties-panel-operator" className="block text-xs font-medium text-navy-300 mb-1">
          Operator
        </label>
        <select
          id="workflow-properties-panel-operator"
          value={formData.operator || ''}
          onChange={(e) => handleChange('operator', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">Select operator...</option>
          <option value="equals">Equals</option>
          <option value="not_equals">Not Equals</option>
          <option value="contains">Contains</option>
          <option value="starts_with">Starts With</option>
          <option value="ends_with">Ends With</option>
          <option value="greater_than">Greater Than</option>
          <option value="less_than">Less Than</option>
        </select>
      </div>

      <div>
        <label htmlFor="workflow-properties-panel-value" className="block text-xs font-medium text-navy-300 mb-1">
          Value
        </label>
        <input
          id="workflow-properties-panel-value"
          type="text"
          value={formData.value || ''}
          onChange={(e) => handleChange('value', e.target.value)}
          placeholder="Comparison value..."
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500"
        />
      </div>
    </>
  );

  const renderActionFields = () => (
    <>
      <div>
        <label htmlFor="workflow-properties-panel-action-type" className="block text-xs font-medium text-navy-300 mb-1">
          Action Type
        </label>
        <select
          id="workflow-properties-panel-action-type"
          value={formData.actionType || ''}
          onChange={(e) => handleChange('actionType', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">Select type...</option>
          <option value="send_message">Send Message</option>
          <option value="call_module">Call Module</option>
          <option value="http_request">HTTP Request</option>
          <option value="delay">Delay</option>
          <option value="webhook">Send Webhook</option>
        </select>
      </div>

      {formData.actionType === 'send_message' && (
        <div>
          <label htmlFor="workflow-properties-panel-message" className="block text-xs font-medium text-navy-300 mb-1">
            Message
          </label>
          <textarea
            id="workflow-properties-panel-message"
            value={formData.message || ''}
            onChange={(e) => handleChange('message', e.target.value)}
            placeholder="Message to send..."
            rows={3}
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500 resize-none"
          />
        </div>
      )}

      {formData.actionType === 'call_module' && (
        <div>
          <label htmlFor="workflow-properties-panel-module" className="block text-xs font-medium text-navy-300 mb-1">
            Module
          </label>
          <select
            id="workflow-properties-panel-module"
            value={formData.module || ''}
            onChange={(e) => handleChange('module', e.target.value)}
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
          >
            <option value="">Select module...</option>
            <option value="ai_interaction">AI Interaction</option>
            <option value="shoutout">Shoutout</option>
            <option value="inventory">Inventory</option>
            <option value="calendar">Calendar</option>
          </select>
        </div>
      )}

      {formData.actionType === 'delay' && (
        <div>
          <label htmlFor="workflow-properties-panel-duration-seconds" className="block text-xs font-medium text-navy-300 mb-1">
            Duration (seconds)
          </label>
          <input
            id="workflow-properties-panel-duration-seconds"
            type="number"
            value={formData.duration || ''}
            onChange={(e) => handleChange('duration', e.target.value)}
            placeholder="5"
            min="0"
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500"
          />
        </div>
      )}
    </>
  );

  const renderDataFields = () => (
    <>
      <div>
        <label htmlFor="workflow-properties-panel-data-type" className="block text-xs font-medium text-navy-300 mb-1">
          Data Type
        </label>
        <select
          id="workflow-properties-panel-data-type"
          value={formData.dataType || ''}
          onChange={(e) => handleChange('dataType', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">Select type...</option>
          <option value="variable">Variable</option>
          <option value="database">Database</option>
          <option value="transform">Transform</option>
          <option value="json">JSON</option>
        </select>
      </div>

      <div>
        <label htmlFor="workflow-properties-panel-source" className="block text-xs font-medium text-navy-300 mb-1">
          Source
        </label>
        <input
          id="workflow-properties-panel-source"
          type="text"
          value={formData.source || ''}
          onChange={(e) => handleChange('source', e.target.value)}
          placeholder="Data source..."
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500"
        />
      </div>
    </>
  );

  const renderLoopFields = () => (
    <>
      <div>
        <label htmlFor="workflow-properties-panel-loop-type" className="block text-xs font-medium text-navy-300 mb-1">
          Loop Type
        </label>
        <select
          id="workflow-properties-panel-loop-type"
          value={formData.loopType || ''}
          onChange={(e) => handleChange('loopType', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">Select type...</option>
          <option value="for_each">For Each</option>
          <option value="while">While</option>
          <option value="repeat">Repeat</option>
        </select>
      </div>

      <div>
        <label htmlFor="workflow-properties-panel-max-iterations" className="block text-xs font-medium text-navy-300 mb-1">
          Max Iterations
        </label>
        <input
          id="workflow-properties-panel-max-iterations"
          type="number"
          value={formData.maxIterations || ''}
          onChange={(e) => handleChange('maxIterations', e.target.value)}
          placeholder="100"
          min="1"
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500"
        />
      </div>
    </>
  );

  const renderFlowFields = () => (
    <>
      <div>
        <label htmlFor="workflow-properties-panel-flow-type" className="block text-xs font-medium text-navy-300 mb-1">
          Flow Type
        </label>
        <select
          id="workflow-properties-panel-flow-type"
          value={formData.flowType || ''}
          onChange={(e) => handleChange('flowType', e.target.value)}
          className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
        >
          <option value="">Select type...</option>
          <option value="merge">Merge</option>
          <option value="split">Split</option>
          <option value="stop">Stop</option>
        </select>
      </div>
    </>
  );

  const renderFields = () => {
    switch (selectedNode.type) {
      case 'trigger':
        return renderTriggerFields();
      case 'condition':
        return renderConditionFields();
      case 'action':
        return renderActionFields();
      case 'data':
        return renderDataFields();
      case 'loop':
        return renderLoopFields();
      case 'flow':
        return renderFlowFields();
      default:
        return null;
    }
  };

  return (
    <div className="w-80 bg-navy-900 border-l border-navy-700 flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-navy-700 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">Node Properties</h3>
        <button
          aria-label="Close"
          onClick={onClose}
          className="p-1 rounded hover:bg-navy-800 text-navy-400 hover:text-sky-300 transition-colors"
        >
          <XMarkIcon className="w-5 h-5" />
        </button>
      </div>

      {/* Form */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Node Label */}
        <div>
          <label htmlFor="workflow-properties-panel-node-label" className="block text-xs font-medium text-navy-300 mb-1">
            Node Label
          </label>
          <input
            id="workflow-properties-panel-node-label"
            type="text"
            value={selectedNode.data.label}
            onChange={(e) =>
              onUpdateNode(selectedNode.id, {
                ...selectedNode.data,
                label: e.target.value,
              })
            }
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm focus:outline-none focus:ring-2 focus:ring-gold-500"
          />
        </div>

        {/* Type-specific fields */}
        {renderFields()}

        {/* Description */}
        <div>
          <label htmlFor="workflow-properties-panel-description" className="block text-xs font-medium text-navy-300 mb-1">
            Description
          </label>
          <textarea
            id="workflow-properties-panel-description"
            value={formData.description || ''}
            onChange={(e) => handleChange('description', e.target.value)}
            placeholder="Optional description..."
            rows={3}
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500 resize-none"
          />
        </div>
      </div>

      {/* Footer */}
      <div className="p-4 border-t border-navy-700">
        <button
          onClick={handleSave}
          className="w-full px-4 py-2 rounded-lg bg-gold-500 hover:bg-gold-600 text-navy-900 font-medium transition-colors"
        >
          Apply Changes
        </button>
      </div>
    </div>
  );
}

export default WorkflowPropertiesPanel;
