import PropTypes from 'prop-types';

// Community type configuration with icons and colors
const communityTypeConfig = {
  creator: {
    icon: '🎬',
    label: 'Creator',
    bgColor: 'bg-purple-500/20',
    borderColor: 'border-purple-500/30',
    textColor: 'text-purple-300',
  },
  gaming: {
    icon: '🎮',
    label: 'Gaming',
    bgColor: 'bg-green-500/20',
    borderColor: 'border-green-500/30',
    textColor: 'text-green-300',
  },
  shared_interest_group: {
    icon: '👥',
    label: 'Interest Group',
    bgColor: 'bg-blue-500/20',
    borderColor: 'border-blue-500/30',
    textColor: 'text-blue-300',
  },
  corporate: {
    icon: '🏢',
    label: 'Corporate',
    bgColor: 'bg-navy-500/20',
    borderColor: 'border-navy-500/30',
    textColor: 'text-fg-secondary',
  },
  other: {
    icon: '📌',
    label: 'Other',
    bgColor: 'bg-orange-500/20',
    borderColor: 'border-orange-500/30',
    textColor: 'text-orange-300',
  },
  workforce: {
    icon: '🏗️',
    label: 'Workforce',
    bgColor: 'bg-cyan-500/20',
    borderColor: 'border-cyan-500/30',
    textColor: 'text-cyan-300',
  },
  support: {
    icon: '🎧',
    label: 'Support',
    bgColor: 'bg-indigo-500/20',
    borderColor: 'border-indigo-500/30',
    textColor: 'text-indigo-300',
  },
};

/**
 * CommunityTypeBadge - Displays a badge with the community type
 * @param {string} type - The community type (creator, gaming, shared_interest_group, corporate, other)
 * @param {string} size - Badge size: 'sm', 'md', 'lg'
 * @param {boolean} showLabel - Whether to show the label text
 * @param {string} className - Additional CSS classes
 */
function CommunityTypeBadge({ type, size = 'sm', showLabel = true, className = '' }) {
  const config = communityTypeConfig[type] || communityTypeConfig.other;

  const sizeClasses = {
    sm: 'text-xs px-2 py-0.5',
    md: 'text-sm px-2.5 py-1',
    lg: 'text-base px-3 py-1.5',
  };

  const iconSizes = {
    sm: 'text-sm',
    md: 'text-base',
    lg: 'text-lg',
  };

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border ${config.bgColor} ${config.borderColor} ${config.textColor} ${sizeClasses[size]} ${className}`}
      title={config.label}
    >
      <span className={iconSizes[size]}>{config.icon}</span>
      {showLabel && <span className="font-medium">{config.label}</span>}
    </span>
  );
}

CommunityTypeBadge.propTypes = {
  type: PropTypes.oneOf(['creator', 'gaming', 'shared_interest_group', 'corporate', 'other', 'workforce', 'support']),
  size: PropTypes.oneOf(['sm', 'md', 'lg']),
  showLabel: PropTypes.bool,
  className: PropTypes.string,
};

// Export the config for use in other components
export { communityTypeConfig };
export default CommunityTypeBadge;
