import { useState, useEffect, useCallback } from 'react';
import { superAdminApi } from '../../services/api';
import {
  Server,
  RefreshCw,
  Check,
  X,
  AlertCircle,
  Clock,
  Activity,
  Wifi,
  WifiOff,
  Settings,
  Play,
  Pause,
  RotateCcw,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Loader2,
  Search,
  Filter,
  Cpu,
  HardDrive,
  MemoryStick,
  Network,
  Container,
  Layers,
  Zap,
} from 'lucide-react';

// Service category configurations
const SERVICE_CATEGORIES = {
  infrastructure: {
    name: 'Infrastructure',
    icon: HardDrive,
    color: 'text-purple-400',
    bgColor: 'bg-purple-500/20',
    borderColor: 'border-purple-500/30',
  },
  core: {
    name: 'Core Modules',
    icon: Cpu,
    color: 'text-blue-400',
    bgColor: 'bg-blue-500/20',
    borderColor: 'border-blue-500/30',
  },
  triggers: {
    name: 'Trigger Receivers',
    icon: Zap,
    color: 'text-yellow-400',
    bgColor: 'bg-yellow-500/20',
    borderColor: 'border-yellow-500/30',
  },
  actions: {
    name: 'Action Modules',
    icon: Play,
    color: 'text-green-400',
    bgColor: 'bg-green-500/20',
    borderColor: 'border-green-500/30',
  },
  processing: {
    name: 'Processing',
    icon: Activity,
    color: 'text-orange-400',
    bgColor: 'bg-orange-500/20',
    borderColor: 'border-orange-500/30',
  },
  admin: {
    name: 'Admin Services',
    icon: Settings,
    color: 'text-sky-400',
    bgColor: 'bg-sky-500/20',
    borderColor: 'border-sky-500/30',
  },
};

// Status badge component
const StatusBadge = ({ status }) => {
  const configs = {
    healthy: { icon: Check, color: 'text-emerald-400', bg: 'bg-emerald-500/20', label: 'Healthy' },
    unhealthy: { icon: X, color: 'text-red-400', bg: 'bg-red-500/20', label: 'Unhealthy' },
    degraded: { icon: AlertCircle, color: 'text-yellow-400', bg: 'bg-yellow-500/20', label: 'Degraded' },
    unknown: { icon: AlertCircle, color: 'text-gray-400', bg: 'bg-gray-500/20', label: 'Unknown' },
    starting: { icon: Loader2, color: 'text-blue-400', bg: 'bg-blue-500/20', label: 'Starting' },
    stopped: { icon: Pause, color: 'text-gray-400', bg: 'bg-gray-500/20', label: 'Stopped' },
  };

  const config = configs[status] || configs.unknown;
  const Icon = config.icon;

  return (
    <span className={`inline-flex items-center space-x-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${config.bg} ${config.color}`}>
      <Icon className={`w-3.5 h-3.5 ${status === 'starting' ? 'animate-spin' : ''}`} />
      <span>{config.label}</span>
    </span>
  );
};

// Response time indicator
const ResponseTimeIndicator = ({ ms }) => {
  if (ms === null || ms === undefined) {
    return <span className="text-gray-500">--</span>;
  }

  let color = 'text-emerald-400';
  if (ms > 500) color = 'text-yellow-400';
  if (ms > 1000) color = 'text-orange-400';
  if (ms > 2000) color = 'text-red-400';

  return (
    <span className={`font-mono text-sm ${color}`}>
      {ms}ms
    </span>
  );
};

// Service card component
const ServiceCard = ({ service, onRefresh, onViewDetails }) => {
  const [isRefreshing, setIsRefreshing] = useState(false);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    await onRefresh(service.id);
    setIsRefreshing(false);
  };

  const category = SERVICE_CATEGORIES[service.category] || SERVICE_CATEGORIES.core;
  const CategoryIcon = category.icon;

  return (
    <div className={`bg-navy-800 rounded-xl border ${
      service.status === 'healthy' ? 'border-navy-700' :
      service.status === 'unhealthy' ? 'border-red-500/50' :
      service.status === 'degraded' ? 'border-yellow-500/50' : 'border-navy-700'
    } overflow-hidden hover:border-navy-600 transition-colors`}>
      <div className="p-4">
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center space-x-3">
            <div className={`p-2 rounded-lg ${category.bgColor}`}>
              <CategoryIcon className={`w-5 h-5 ${category.color}`} />
            </div>
            <div>
              <h3 className="font-semibold text-white">{service.name}</h3>
              <p className="text-xs text-navy-400">{service.description || service.url}</p>
            </div>
          </div>
          <StatusBadge status={service.status} />
        </div>

        {/* Metrics */}
        <div className="grid grid-cols-3 gap-3 mb-3">
          <div className="bg-navy-900/50 rounded-lg p-2">
            <div className="text-xs text-navy-400 mb-1">Response</div>
            <ResponseTimeIndicator ms={service.responseTime} />
          </div>
          <div className="bg-navy-900/50 rounded-lg p-2">
            <div className="text-xs text-navy-400 mb-1">Uptime</div>
            <span className="text-sm text-white font-medium">
              {service.uptime ? `${service.uptime}%` : '--'}
            </span>
          </div>
          <div className="bg-navy-900/50 rounded-lg p-2">
            <div className="text-xs text-navy-400 mb-1">Version</div>
            <span className="text-sm text-white font-mono">
              {service.version || '--'}
            </span>
          </div>
        </div>

        {/* Last checked */}
        <div className="flex items-center justify-between text-xs">
          <span className="text-navy-400 flex items-center space-x-1">
            <Clock className="w-3.5 h-3.5" />
            <span>
              {service.lastChecked
                ? new Date(service.lastChecked).toLocaleTimeString()
                : 'Never checked'}
            </span>
          </span>
          <div className="flex items-center space-x-2">
            <button
              onClick={handleRefresh}
              disabled={isRefreshing}
              className="p-1.5 rounded-lg hover:bg-navy-700 text-navy-400 hover:text-white transition-colors disabled:opacity-50"
              title="Refresh status"
            >
              <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={() => onViewDetails(service)}
              className="p-1.5 rounded-lg hover:bg-navy-700 text-navy-400 hover:text-white transition-colors"
              title="View details"
            >
              <ExternalLink className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Health endpoint indicator */}
      {service.healthEndpoint && (
        <div className="px-4 py-2 bg-navy-900/50 border-t border-navy-700">
          <div className="flex items-center space-x-2 text-xs text-navy-400">
            <Network className="w-3.5 h-3.5" />
            <span className="font-mono truncate">{service.healthEndpoint}</span>
          </div>
        </div>
      )}
    </div>
  );
};

// Category section component
const CategorySection = ({ category, services, onRefresh, onViewDetails }) => {
  const [isExpanded, setIsExpanded] = useState(true);
  const config = SERVICE_CATEGORIES[category] || SERVICE_CATEGORIES.core;
  const Icon = config.icon;

  const healthyCount = services.filter(s => s.status === 'healthy').length;
  const totalCount = services.length;

  return (
    <div className="mb-6">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between mb-3 group"
      >
        <div className="flex items-center space-x-3">
          <div className={`p-2 rounded-lg ${config.bgColor}`}>
            <Icon className={`w-5 h-5 ${config.color}`} />
          </div>
          <div className="text-left">
            <h2 className="text-lg font-semibold text-white group-hover:text-sky-400 transition-colors">
              {config.name}
            </h2>
            <p className="text-xs text-navy-400">
              {healthyCount}/{totalCount} healthy
            </p>
          </div>
        </div>
        <div className="flex items-center space-x-3">
          <div className={`h-2 w-24 rounded-full bg-navy-700 overflow-hidden`}>
            <div
              className={`h-full rounded-full transition-all ${
                healthyCount === totalCount ? 'bg-emerald-500' :
                healthyCount > 0 ? 'bg-yellow-500' : 'bg-red-500'
              }`}
              style={{ width: `${(healthyCount / totalCount) * 100}%` }}
            />
          </div>
          {isExpanded ? (
            <ChevronDown className="w-5 h-5 text-navy-400" />
          ) : (
            <ChevronRight className="w-5 h-5 text-navy-400" />
          )}
        </div>
      </button>

      {isExpanded && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {services.map(service => (
            <ServiceCard
              key={service.id}
              service={service}
              onRefresh={onRefresh}
              onViewDetails={onViewDetails}
            />
          ))}
        </div>
      )}
    </div>
  );
};

// Service details modal
const ServiceDetailsModal = ({ service, onClose }) => {
  if (!service) return null;

  const category = SERVICE_CATEGORIES[service.category] || SERVICE_CATEGORIES.core;

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-navy-800 rounded-2xl border border-navy-700 w-full max-w-2xl max-h-[80vh] overflow-hidden">
        <div className="p-6 border-b border-navy-700">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              <div className={`p-3 rounded-xl ${category.bgColor}`}>
                <Server className={`w-6 h-6 ${category.color}`} />
              </div>
              <div>
                <h2 className="text-xl font-bold text-white">{service.name}</h2>
                <p className="text-sm text-navy-400">{service.description}</p>
              </div>
            </div>
            <button
              onClick={onClose}
              className="p-2 rounded-lg hover:bg-navy-700 text-navy-400 hover:text-white transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        <div className="p-6 overflow-y-auto max-h-[60vh] space-y-6">
          {/* Status Overview */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-navy-900/50 rounded-xl p-4">
              <div className="text-xs text-navy-400 mb-1">Status</div>
              <StatusBadge status={service.status} />
            </div>
            <div className="bg-navy-900/50 rounded-xl p-4">
              <div className="text-xs text-navy-400 mb-1">Response Time</div>
              <ResponseTimeIndicator ms={service.responseTime} />
            </div>
            <div className="bg-navy-900/50 rounded-xl p-4">
              <div className="text-xs text-navy-400 mb-1">Uptime</div>
              <span className="text-lg font-semibold text-white">
                {service.uptime ? `${service.uptime}%` : '--'}
              </span>
            </div>
            <div className="bg-navy-900/50 rounded-xl p-4">
              <div className="text-xs text-navy-400 mb-1">Version</div>
              <span className="text-lg font-mono text-white">
                {service.version || '--'}
              </span>
            </div>
          </div>

          {/* Connection Details */}
          <div className="bg-navy-900/50 rounded-xl p-4">
            <h3 className="text-sm font-semibold text-white mb-3">Connection Details</h3>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-sm text-navy-400">URL</span>
                <span className="text-sm font-mono text-white">{service.url}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-navy-400">Health Endpoint</span>
                <span className="text-sm font-mono text-white">{service.healthEndpoint || '/health'}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-navy-400">Port</span>
                <span className="text-sm font-mono text-white">{service.port || '--'}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-sm text-navy-400">Protocol</span>
                <span className="text-sm font-mono text-white">{service.protocol || 'HTTP'}</span>
              </div>
            </div>
          </div>

          {/* Dependencies */}
          {service.dependencies && service.dependencies.length > 0 && (
            <div className="bg-navy-900/50 rounded-xl p-4">
              <h3 className="text-sm font-semibold text-white mb-3">Dependencies</h3>
              <div className="flex flex-wrap gap-2">
                {service.dependencies.map((dep, index) => (
                  <span
                    key={index}
                    className="px-2 py-1 bg-navy-700 rounded-lg text-xs text-navy-300"
                  >
                    {dep}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Environment Info */}
          {service.environment && (
            <div className="bg-navy-900/50 rounded-xl p-4">
              <h3 className="text-sm font-semibold text-white mb-3">Environment</h3>
              <div className="space-y-2">
                {Object.entries(service.environment).map(([key, value]) => (
                  <div key={key} className="flex items-center justify-between">
                    <span className="text-sm text-navy-400">{key}</span>
                    <span className="text-sm font-mono text-white">{value}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Recent Events */}
          {service.recentEvents && service.recentEvents.length > 0 && (
            <div className="bg-navy-900/50 rounded-xl p-4">
              <h3 className="text-sm font-semibold text-white mb-3">Recent Events</h3>
              <div className="space-y-2">
                {service.recentEvents.map((event, index) => (
                  <div
                    key={index}
                    className="flex items-center justify-between py-2 border-b border-navy-700 last:border-0"
                  >
                    <div className="flex items-center space-x-2">
                      {event.type === 'error' ? (
                        <X className="w-4 h-4 text-red-400" />
                      ) : event.type === 'warning' ? (
                        <AlertCircle className="w-4 h-4 text-yellow-400" />
                      ) : (
                        <Check className="w-4 h-4 text-emerald-400" />
                      )}
                      <span className="text-sm text-navy-300">{event.message}</span>
                    </div>
                    <span className="text-xs text-navy-500">
                      {new Date(event.timestamp).toLocaleString()}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

// Main component
export default function SuperAdminServiceDiscovery() {
  const [services, setServices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [selectedService, setSelectedService] = useState(null);
  const [isRefreshingAll, setIsRefreshingAll] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);

  // Fetch services
  const fetchServices = useCallback(async () => {
    try {
      const response = await superAdminApi.getServices();
      setServices(response.data.services || []);
      setError(null);
    } catch (err) {
      console.error('Failed to fetch services:', err);
      setError('Failed to load services. Please try again.');

      // Use mock data for development
      setServices(getMockServices());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchServices();
  }, [fetchServices]);

  // Auto-refresh
  useEffect(() => {
    if (!autoRefresh) return;

    const interval = setInterval(fetchServices, 30000);
    return () => clearInterval(interval);
  }, [autoRefresh, fetchServices]);

  // Refresh single service
  const handleRefreshService = async (serviceId) => {
    try {
      await superAdminApi.refreshService(serviceId);
      await fetchServices();
    } catch (err) {
      console.error('Failed to refresh service:', err);
    }
  };

  // Refresh all services
  const handleRefreshAll = async () => {
    setIsRefreshingAll(true);
    try {
      await superAdminApi.refreshAllServices();
      await fetchServices();
    } catch (err) {
      console.error('Failed to refresh all services:', err);
    } finally {
      setIsRefreshingAll(false);
    }
  };

  // Filter services
  const filteredServices = services.filter(service => {
    const matchesSearch =
      service.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      service.description?.toLowerCase().includes(searchTerm.toLowerCase());
    const matchesStatus = statusFilter === 'all' || service.status === statusFilter;
    return matchesSearch && matchesStatus;
  });

  // Group by category
  const servicesByCategory = filteredServices.reduce((acc, service) => {
    const category = service.category || 'core';
    if (!acc[category]) acc[category] = [];
    acc[category].push(service);
    return acc;
  }, {});

  // Calculate stats
  const stats = {
    total: services.length,
    healthy: services.filter(s => s.status === 'healthy').length,
    unhealthy: services.filter(s => s.status === 'unhealthy').length,
    degraded: services.filter(s => s.status === 'degraded').length,
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="flex flex-col items-center space-y-4">
          <Loader2 className="w-8 h-8 text-sky-400 animate-spin" />
          <p className="text-navy-400">Loading services...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center space-x-3">
            <Layers className="w-7 h-7 text-sky-400" />
            <span>Service Discovery</span>
          </h1>
          <p className="text-navy-400 mt-1">
            Monitor and manage Waddles microservices
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => setAutoRefresh(!autoRefresh)}
            className={`flex items-center space-x-2 px-4 py-2 rounded-xl transition-colors ${
              autoRefresh
                ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                : 'bg-navy-800 text-navy-300 border border-navy-700 hover:border-navy-600'
            }`}
          >
            {autoRefresh ? <Wifi className="w-4 h-4" /> : <WifiOff className="w-4 h-4" />}
            <span className="text-sm">Auto-refresh</span>
          </button>

          <button
            onClick={handleRefreshAll}
            disabled={isRefreshingAll}
            className="flex items-center space-x-2 px-4 py-2 bg-sky-500 hover:bg-sky-600 text-white rounded-xl transition-colors disabled:opacity-50"
          >
            <RotateCcw className={`w-4 h-4 ${isRefreshingAll ? 'animate-spin' : ''}`} />
            <span>Refresh All</span>
          </button>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-navy-800 rounded-xl border border-navy-700 p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-navy-400">Total Services</p>
              <p className="text-2xl font-bold text-white">{stats.total}</p>
            </div>
            <div className="p-3 bg-sky-500/20 rounded-xl">
              <Server className="w-6 h-6 text-sky-400" />
            </div>
          </div>
        </div>

        <div className="bg-navy-800 rounded-xl border border-navy-700 p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-navy-400">Healthy</p>
              <p className="text-2xl font-bold text-emerald-400">{stats.healthy}</p>
            </div>
            <div className="p-3 bg-emerald-500/20 rounded-xl">
              <Check className="w-6 h-6 text-emerald-400" />
            </div>
          </div>
        </div>

        <div className="bg-navy-800 rounded-xl border border-navy-700 p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-navy-400">Degraded</p>
              <p className="text-2xl font-bold text-yellow-400">{stats.degraded}</p>
            </div>
            <div className="p-3 bg-yellow-500/20 rounded-xl">
              <AlertCircle className="w-6 h-6 text-yellow-400" />
            </div>
          </div>
        </div>

        <div className="bg-navy-800 rounded-xl border border-navy-700 p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-navy-400">Unhealthy</p>
              <p className="text-2xl font-bold text-red-400">{stats.unhealthy}</p>
            </div>
            <div className="p-3 bg-red-500/20 rounded-xl">
              <X className="w-6 h-6 text-red-400" />
            </div>
          </div>
        </div>
      </div>

      {/* Search and Filters */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-navy-400" />
          <input
            type="text"
            placeholder="Search services..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full pl-10 pr-4 py-2.5 bg-navy-800 border border-navy-700 rounded-xl text-white placeholder-navy-400 focus:outline-none focus:border-sky-500"
          />
        </div>

        <div className="flex items-center space-x-2">
          <Filter className="w-5 h-5 text-navy-400" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="px-4 py-2.5 bg-navy-800 border border-navy-700 rounded-xl text-white focus:outline-none focus:border-sky-500"
          >
            <option value="all">All Status</option>
            <option value="healthy">Healthy</option>
            <option value="degraded">Degraded</option>
            <option value="unhealthy">Unhealthy</option>
            <option value="unknown">Unknown</option>
          </select>
        </div>
      </div>

      {/* Error Alert */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4">
          <div className="flex items-center space-x-3">
            <AlertCircle className="w-5 h-5 text-red-400 flex-shrink-0" />
            <p className="text-red-400">{error}</p>
          </div>
        </div>
      )}

      {/* Service Categories */}
      {Object.entries(SERVICE_CATEGORIES).map(([category]) => {
        const categoryServices = servicesByCategory[category];
        if (!categoryServices || categoryServices.length === 0) return null;

        return (
          <CategorySection
            key={category}
            category={category}
            services={categoryServices}
            onRefresh={handleRefreshService}
            onViewDetails={setSelectedService}
          />
        );
      })}

      {/* Empty State */}
      {filteredServices.length === 0 && (
        <div className="text-center py-12">
          <Server className="w-12 h-12 text-navy-600 mx-auto mb-4" />
          <h3 className="text-lg font-semibold text-white mb-2">No services found</h3>
          <p className="text-navy-400">
            {searchTerm || statusFilter !== 'all'
              ? 'Try adjusting your search or filter criteria'
              : 'No services are registered yet'}
          </p>
        </div>
      )}

      {/* Service Details Modal */}
      <ServiceDetailsModal
        service={selectedService}
        onClose={() => setSelectedService(null)}
      />
    </div>
  );
}

// Mock data for development
function getMockServices() {
  return [
    // Infrastructure
    {
      id: 'postgres',
      name: 'PostgreSQL',
      description: 'Primary database',
      category: 'infrastructure',
      status: 'healthy',
      url: 'waddlebot-postgres:5432',
      healthEndpoint: null,
      responseTime: 12,
      uptime: 99.9,
      version: '15-alpine',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'redis',
      name: 'Redis',
      description: 'Cache and session store',
      category: 'infrastructure',
      status: 'healthy',
      url: 'waddlebot-redis:6379',
      healthEndpoint: null,
      responseTime: 3,
      uptime: 99.9,
      version: '7-alpine',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'seaweedfs',
      name: 'SeaweedFS',
      description: 'Object storage',
      category: 'infrastructure',
      status: 'healthy',
      url: 'localhost:8333',
      healthEndpoint: '/cluster/status',
      responseTime: 45,
      uptime: 99.5,
      version: '4.48',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'ollama',
      name: 'Ollama',
      description: 'Local AI inference',
      category: 'infrastructure',
      status: 'healthy',
      url: 'waddlebot-ollama:11434',
      healthEndpoint: '/api/tags',
      responseTime: 89,
      uptime: 98.5,
      version: 'latest',
      lastChecked: new Date().toISOString(),
    },

    // Core modules
    {
      id: 'hub',
      name: 'Hub Module',
      description: 'Central admin dashboard',
      category: 'admin',
      status: 'healthy',
      url: 'localhost:8060',
      healthEndpoint: '/api/v1/health',
      responseTime: 125,
      uptime: 99.8,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
      dependencies: ['postgres', 'redis'],
    },
    {
      id: 'identity-core',
      name: 'Identity Core',
      description: 'User identity management',
      category: 'core',
      status: 'healthy',
      url: 'localhost:8051',
      healthEndpoint: '/health',
      responseTime: 78,
      uptime: 99.7,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
      dependencies: ['postgres'],
    },
    {
      id: 'browser-source',
      name: 'Browser Source',
      description: 'OBS overlay endpoints',
      category: 'core',
      status: 'healthy',
      url: 'localhost:8052',
      healthEndpoint: '/health',
      responseTime: 56,
      uptime: 99.9,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'labels-core',
      name: 'Labels Core',
      description: 'Label management',
      category: 'core',
      status: 'healthy',
      url: 'localhost:8053',
      healthEndpoint: '/health',
      responseTime: 42,
      uptime: 99.6,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'workflow-core',
      name: 'Workflow Core',
      description: 'Workflow automation engine',
      category: 'core',
      status: 'degraded',
      url: 'localhost:8054',
      healthEndpoint: '/health',
      responseTime: 234,
      uptime: 97.5,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
      dependencies: ['postgres', 'redis'],
    },
    {
      id: 'unified-music',
      name: 'Unified Music',
      description: 'Music provider integration',
      category: 'core',
      status: 'healthy',
      url: 'localhost:8055',
      healthEndpoint: '/health',
      responseTime: 112,
      uptime: 99.2,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },

    // Trigger receivers
    {
      id: 'twitch-receiver',
      name: 'Twitch Receiver',
      description: 'Twitch chat and events',
      category: 'triggers',
      status: 'healthy',
      url: 'localhost:8010',
      healthEndpoint: '/health',
      responseTime: 89,
      uptime: 99.4,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'discord-receiver',
      name: 'Discord Receiver',
      description: 'Discord bot integration',
      category: 'triggers',
      status: 'healthy',
      url: 'localhost:8011',
      healthEndpoint: '/health',
      responseTime: 67,
      uptime: 99.6,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'slack-receiver',
      name: 'Slack Receiver',
      description: 'Slack bot integration',
      category: 'triggers',
      status: 'unhealthy',
      url: 'localhost:8012',
      healthEndpoint: '/health',
      responseTime: null,
      uptime: 85.2,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },

    // Processing
    {
      id: 'router',
      name: 'Router Module',
      description: 'Message routing and processing',
      category: 'processing',
      status: 'healthy',
      url: 'localhost:8030',
      healthEndpoint: '/health',
      responseTime: 45,
      uptime: 99.9,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
      dependencies: ['redis', 'postgres'],
    },

    // Action modules
    {
      id: 'ai-interaction',
      name: 'AI Interaction',
      description: 'AI chat responses',
      category: 'actions',
      status: 'healthy',
      url: 'localhost:8020',
      healthEndpoint: '/health',
      responseTime: 234,
      uptime: 98.8,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
      dependencies: ['ollama'],
    },
    {
      id: 'loyalty-interaction',
      name: 'Loyalty System',
      description: 'Points and rewards',
      category: 'actions',
      status: 'healthy',
      url: 'localhost:8021',
      healthEndpoint: '/health',
      responseTime: 56,
      uptime: 99.5,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },
    {
      id: 'inventory-interaction',
      name: 'Inventory System',
      description: 'Virtual items and trading',
      category: 'actions',
      status: 'healthy',
      url: 'localhost:8022',
      healthEndpoint: '/health',
      responseTime: 78,
      uptime: 99.3,
      version: '1.0.0',
      lastChecked: new Date().toISOString(),
    },
  ];
}
