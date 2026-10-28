import { useState, useEffect } from 'react';
import {
  PlayIcon,
  XMarkIcon,
  CheckCircleIcon,
  XCircleIcon,
  ClockIcon,
  ChevronDownIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline';

function WorkflowTestPanel({ workflowId, onClose, onHighlightNode }) {
  const [isRunning, setIsRunning] = useState(false);
  const [executionData, setExecutionData] = useState(null);
  const [testInput, setTestInput] = useState('');
  const [expandedSteps, setExpandedSteps] = useState({});

  const handleRunTest = async () => {
    setIsRunning(true);

    // Simulate test execution
    const mockExecution = {
      id: 'exec_' + Date.now(),
      status: 'running',
      startTime: new Date().toISOString(),
      steps: [],
    };

    setExecutionData(mockExecution);

    // Simulate step-by-step execution
    const steps = [
      { nodeId: 'node-1', status: 'completed', duration: 50, output: { message: 'Trigger received' } },
      { nodeId: 'node-2', status: 'completed', duration: 100, output: { condition: true } },
      { nodeId: 'node-3', status: 'completed', duration: 200, output: { response: 'Action executed' } },
    ];

    for (let i = 0; i < steps.length; i++) {
      await new Promise((resolve) => setTimeout(resolve, 500));

      mockExecution.steps.push(steps[i]);
      setExecutionData({ ...mockExecution, steps: [...mockExecution.steps] });

      if (onHighlightNode) {
        onHighlightNode(steps[i].nodeId);
      }
    }

    mockExecution.status = 'completed';
    mockExecution.endTime = new Date().toISOString();
    setExecutionData({ ...mockExecution });
    setIsRunning(false);
  };

  const toggleStep = (index) => {
    setExpandedSteps((prev) => ({
      ...prev,
      [index]: !prev[index],
    }));
  };

  const getStatusIcon = (status) => {
    switch (status) {
      case 'completed':
        return <CheckCircleIcon className="w-5 h-5 text-green-400" />;
      case 'failed':
        return <XCircleIcon className="w-5 h-5 text-red-400" />;
      case 'running':
        return <ClockIcon className="w-5 h-5 text-yellow-400 animate-spin" />;
      default:
        return <ClockIcon className="w-5 h-5 text-navy-500" />;
    }
  };

  const getStatusColor = (status) => {
    switch (status) {
      case 'completed':
        return 'text-green-400 bg-green-500/20';
      case 'failed':
        return 'text-red-400 bg-red-500/20';
      case 'running':
        return 'text-yellow-400 bg-yellow-500/20';
      default:
        return 'text-navy-400 bg-navy-700';
    }
  };

  return (
    <div className="w-96 bg-navy-900 border-l border-navy-700 flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-navy-700">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-white">Test Workflow</h3>
          <button
            aria-label="Close"
            onClick={onClose}
            className="p-1 rounded hover:bg-navy-800 text-navy-400 hover:text-sky-300 transition-colors"
          >
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>

        {/* Test Input */}
        <div className="space-y-2">
          <label htmlFor="workflow-test-panel-test-input-json" className="block text-xs font-medium text-navy-300">
            Test Input (JSON)
          </label>
          <textarea
            id="workflow-test-panel-test-input-json"
            value={testInput}
            onChange={(e) => setTestInput(e.target.value)}
            placeholder='{"message": "test", "user": "testuser"}'
            rows={4}
            className="w-full px-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-xs font-mono placeholder-navy-500 focus:outline-none focus:ring-2 focus:ring-gold-500 resize-none"
          />
        </div>

        {/* Run Button */}
        <button
          onClick={handleRunTest}
          disabled={isRunning}
          className="w-full mt-3 flex items-center justify-center space-x-2 px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-700 text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <PlayIcon className="w-4 h-4" />
          <span className="text-sm">{isRunning ? 'Running...' : 'Run Test'}</span>
        </button>
      </div>

      {/* Execution Results */}
      <div className="flex-1 overflow-y-auto">
        {!executionData ? (
          <div className="p-8 text-center">
            <div className="w-16 h-16 rounded-full bg-navy-800 flex items-center justify-center mx-auto mb-4">
              <PlayIcon className="w-8 h-8 text-navy-600" />
            </div>
            <p className="text-sm text-navy-400">
              Click "Run Test" to execute the workflow
            </p>
          </div>
        ) : (
          <div className="p-4 space-y-4">
            {/* Execution Summary */}
            <div className="p-3 bg-navy-800 rounded-lg border border-navy-700">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-navy-300">Status</span>
                <span
                  className={`text-xs px-2 py-1 rounded font-medium ${getStatusColor(
                    executionData.status
                  )}`}
                >
                  {executionData.status}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-navy-300">Duration</span>
                <span className="text-xs text-white">
                  {executionData.endTime
                    ? `${Math.round(
                        (new Date(executionData.endTime) - new Date(executionData.startTime)) / 1000
                      )}s`
                    : 'Running...'}
                </span>
              </div>
            </div>

            {/* Execution Steps */}
            <div>
              <h4 className="text-xs font-semibold text-navy-300 mb-2">Execution Steps</h4>
              <div className="space-y-2">
                {executionData.steps.map((step, index) => (
                  <div
                    key={index}
                    className="border border-navy-700 rounded-lg overflow-hidden bg-navy-800"
                  >
                    <button
                      onClick={() => toggleStep(index)}
                      className="w-full px-3 py-2 flex items-center justify-between hover:bg-navy-750 transition-colors"
                    >
                      <div className="flex items-center space-x-2">
                        {getStatusIcon(step.status)}
                        <span className="text-sm text-white">Step {index + 1}</span>
                        <span className="text-xs text-navy-400">{step.duration}ms</span>
                      </div>
                      {expandedSteps[index] ? (
                        <ChevronDownIcon className="w-4 h-4 text-navy-400" />
                      ) : (
                        <ChevronRightIcon className="w-4 h-4 text-navy-400" />
                      )}
                    </button>

                    {expandedSteps[index] && (
                      <div className="px-3 py-2 border-t border-navy-700 bg-navy-850">
                        <div className="mb-2">
                          <span className="text-xs font-medium text-navy-300">Node ID:</span>
                          <span className="text-xs text-sky-400 ml-2">{step.nodeId}</span>
                        </div>
                        {step.output && (
                          <div>
                            <span className="text-xs font-medium text-navy-300 block mb-1">
                              Output:
                            </span>
                            <pre className="text-xs text-white bg-navy-900 p-2 rounded border border-navy-700 overflow-x-auto">
                              {JSON.stringify(step.output, null, 2)}
                            </pre>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>

            {/* Final Output */}
            {executionData.status === 'completed' && (
              <div>
                <h4 className="text-xs font-semibold text-navy-300 mb-2">Final Output</h4>
                <div className="p-3 bg-navy-800 rounded-lg border border-navy-700">
                  <pre className="text-xs text-white overflow-x-auto">
                    {JSON.stringify(
                      {
                        success: true,
                        message: 'Workflow executed successfully',
                        executionTime:
                          new Date(executionData.endTime) - new Date(executionData.startTime),
                      },
                      null,
                      2
                    )}
                  </pre>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Footer Tips */}
      <div className="p-4 border-t border-navy-700 bg-navy-850">
        <div className="text-xs text-navy-500">
          <div className="font-semibold mb-1">Test Tips:</div>
          <ul className="list-disc list-inside space-y-1">
            <li>Provide sample input data</li>
            <li>Watch node execution in real-time</li>
            <li>Check output at each step</li>
          </ul>
        </div>
      </div>
    </div>
  );
}

export default WorkflowTestPanel;
