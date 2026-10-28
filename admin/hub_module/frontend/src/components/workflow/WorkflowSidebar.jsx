import { useState } from 'react';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  FolderIcon,
  DocumentDuplicateIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';

function WorkflowSidebar({ workflows, activeWorkflowId, onSelectWorkflow, onCreateWorkflow, onDeleteWorkflow }) {
  const [searchQuery, setSearchQuery] = useState('');

  const filteredWorkflows = workflows.filter((workflow) =>
    workflow.name.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="w-80 bg-navy-900 border-r border-navy-700 flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-navy-700">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-white">Workflows</h2>
          <button
            onClick={onCreateWorkflow}
            className="p-1.5 rounded-lg bg-gold-500 hover:bg-gold-600 text-navy-900 transition-colors"
            title="Create New Workflow"
          >
            <PlusIcon className="w-5 h-5" />
          </button>
        </div>

        {/* Search */}
        <div className="relative">
          <MagnifyingGlassIcon className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-navy-400" />
          <input
            aria-label="Search workflows"
            type="text"
            placeholder="Search workflows..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-10 pr-3 py-2 bg-navy-800 border border-navy-700 rounded-lg text-white text-sm placeholder-navy-400 focus:outline-none focus:ring-2 focus:ring-gold-500 focus:border-transparent"
          />
        </div>
      </div>

      {/* Workflow List */}
      <div className="flex-1 overflow-y-auto">
        {filteredWorkflows.length === 0 ? (
          <div className="p-8 text-center">
            <FolderIcon className="w-12 h-12 text-navy-600 mx-auto mb-2" />
            <p className="text-sm text-navy-400">No workflows found</p>
          </div>
        ) : (
          <div className="p-2 space-y-1">
            {filteredWorkflows.map((workflow) => (
              <div
                key={workflow.id}
                data-click-delegate
                className={`p-3 rounded-lg cursor-pointer transition-colors group ${
                  activeWorkflowId === workflow.id
                    ? 'bg-gold-500/20 border border-gold-500/30'
                    : 'hover:bg-navy-800 border border-transparent'
                }`}
                onClick={() => onSelectWorkflow(workflow.id)}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center space-x-2 mb-1">
                      <DocumentDuplicateIcon className="w-4 h-4 text-navy-400 flex-shrink-0" />
                      <h3 className="text-sm font-medium text-white truncate">
                        <button type="button" aria-current={activeWorkflowId === workflow.id ? "true" : undefined} className="max-w-full truncate text-left hover:underline focus-visible:underline">{workflow.name}</button>
                      </h3>
                    </div>
                    {workflow.description && (
                      <p className="text-xs text-navy-400 line-clamp-2 ml-6">
                        {workflow.description}
                      </p>
                    )}
                    <div className="flex items-center space-x-2 mt-2 ml-6">
                      <span
                        className={`text-xs px-2 py-0.5 rounded ${
                          workflow.status === 'published'
                            ? 'bg-green-500/20 text-green-400'
                            : workflow.status === 'draft'
                              ? 'bg-yellow-500/20 text-yellow-400'
                              : 'bg-navy-700 text-navy-400'
                        }`}
                      >
                        {workflow.status}
                      </span>
                      {workflow.triggerCount > 0 && (
                        <span className="text-xs text-navy-500">
                          {workflow.triggerCount} triggers
                        </span>
                      )}
                    </div>
                  </div>

                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDeleteWorkflow(workflow.id);
                    }}
                    className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-red-500/20 text-navy-400 hover:text-red-400 transition-all"
                    title="Delete Workflow"
                  >
                    <TrashIcon className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default WorkflowSidebar;
