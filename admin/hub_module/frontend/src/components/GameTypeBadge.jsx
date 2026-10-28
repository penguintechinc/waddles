import PropTypes from 'prop-types';

// Game type configuration with logos and colors
const gameTypeConfig = {
  rust: {
    logo: '/assets/games/rust.svg',
    label: 'Rust',
    bgColor: 'bg-red-500/20',
    borderColor: 'border-red-500/30',
    textColor: 'text-red-300',
  },
  minecraft: {
    logo: '/assets/games/minecraft.svg',
    label: 'Minecraft',
    bgColor: 'bg-green-500/20',
    borderColor: 'border-green-500/30',
    textColor: 'text-green-300',
  },
  cs2: {
    logo: '/assets/games/cs2.svg',
    label: 'CS2',
    bgColor: 'bg-amber-500/20',
    borderColor: 'border-amber-500/30',
    textColor: 'text-amber-300',
  },
  ark: {
    logo: '/assets/games/ark.svg',
    label: 'ARK',
    bgColor: 'bg-teal-500/20',
    borderColor: 'border-teal-500/30',
    textColor: 'text-teal-300',
  },
  valheim: {
    logo: '/assets/games/valheim.svg',
    label: 'Valheim',
    bgColor: 'bg-blue-500/20',
    borderColor: 'border-blue-500/30',
    textColor: 'text-blue-300',
  },
  palworld: {
    logo: '/assets/games/palworld.svg',
    label: 'Palworld',
    bgColor: 'bg-cyan-500/20',
    borderColor: 'border-cyan-500/30',
    textColor: 'text-cyan-300',
  },
  factorio: {
    logo: '/assets/games/factorio.svg',
    label: 'Factorio',
    bgColor: 'bg-orange-500/20',
    borderColor: 'border-orange-500/30',
    textColor: 'text-orange-300',
  },
  conan_exiles: {
    logo: '/assets/games/conan_exiles.svg',
    label: 'Conan Exiles',
    bgColor: 'bg-yellow-500/20',
    borderColor: 'border-yellow-500/30',
    textColor: 'text-yellow-300',
  },
  '7dtd': {
    logo: '/assets/games/7dtd.svg',
    label: '7 Days to Die',
    bgColor: 'bg-rose-500/20',
    borderColor: 'border-rose-500/30',
    textColor: 'text-rose-300',
  },
  squad: {
    logo: '/assets/games/squad.svg',
    label: 'Squad',
    bgColor: 'bg-emerald-500/20',
    borderColor: 'border-emerald-500/30',
    textColor: 'text-emerald-300',
  },
  unturned: {
    logo: '/assets/games/unturned.svg',
    label: 'Unturned',
    bgColor: 'bg-lime-500/20',
    borderColor: 'border-lime-500/30',
    textColor: 'text-lime-300',
  },
  terraria: {
    logo: '/assets/games/terraria.svg',
    label: 'Terraria',
    bgColor: 'bg-violet-500/20',
    borderColor: 'border-violet-500/30',
    textColor: 'text-violet-300',
  },
  starbound: {
    logo: '/assets/games/starbound.svg',
    label: 'Starbound',
    bgColor: 'bg-fuchsia-500/20',
    borderColor: 'border-fuchsia-500/30',
    textColor: 'text-fuchsia-300',
  },
  source: {
    logo: '/assets/games/source.svg',
    label: 'Source',
    bgColor: 'bg-amber-500/20',
    borderColor: 'border-amber-500/30',
    textColor: 'text-amber-300',
  },
  mumble: {
    logo: '/assets/games/mumble.svg',
    label: 'Mumble',
    bgColor: 'bg-indigo-500/20',
    borderColor: 'border-indigo-500/30',
    textColor: 'text-indigo-300',
  },
  teamspeak: {
    logo: '/assets/games/teamspeak.svg',
    label: 'TeamSpeak',
    bgColor: 'bg-sky-500/20',
    borderColor: 'border-sky-500/30',
    textColor: 'text-sky-300',
  },
  other: {
    logo: null,
    label: 'Other',
    bgColor: 'bg-navy-500/20',
    borderColor: 'border-navy-500/30',
    textColor: 'text-fg-secondary',
  },
};

// Fallback SVG icon when no logo file is available
function ServerFallbackIcon({ className = 'w-4 h-4' }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 17.25v-.228a4.5 4.5 0 0 0-.12-1.03l-2.268-9.64a3.375 3.375 0 0 0-3.285-2.602H7.923a3.375 3.375 0 0 0-3.285 2.602l-2.268 9.64a4.5 4.5 0 0 0-.12 1.03v.228m19.5 0a3 3 0 0 1-3 3H5.25a3 3 0 0 1-3-3m19.5 0a3 3 0 0 0-3-3H5.25a3 3 0 0 0-3 3m16.5 0h.008v.008h-.008v-.008Zm-3 0h.008v.008h-.008v-.008Z" />
    </svg>
  );
}

/**
 * GameTypeBadge - Displays a badge with the game type logo and label
 */
function GameTypeBadge({ type, size = 'sm', showLabel = true, className = '' }) {
  const config = gameTypeConfig[type] || gameTypeConfig.other;

  const sizeClasses = {
    sm: 'text-xs px-2 py-0.5',
    md: 'text-sm px-2.5 py-1',
    lg: 'text-base px-3 py-1.5',
  };

  const iconSizes = {
    sm: 'w-3.5 h-3.5',
    md: 'w-4 h-4',
    lg: 'w-5 h-5',
  };

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border ${config.bgColor} ${config.borderColor} ${config.textColor} ${sizeClasses[size]} ${className}`}
      title={config.label}
    >
      {config.logo ? (
        <img
          src={config.logo}
          alt={config.label}
          className={`${iconSizes[size]} object-contain`}
          onError={(e) => { e.target.style.display = 'none'; }}
        />
      ) : (
        <ServerFallbackIcon className={iconSizes[size]} />
      )}
      {showLabel && <span className="font-medium">{config.label}</span>}
    </span>
  );
}

GameTypeBadge.propTypes = {
  type: PropTypes.string,
  size: PropTypes.oneOf(['sm', 'md', 'lg']),
  showLabel: PropTypes.bool,
  className: PropTypes.string,
};

export { gameTypeConfig };
export default GameTypeBadge;
