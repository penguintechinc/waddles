export default {
  testEnvironment: 'node',
  transform: {},
  testMatch: ['**/__tests__/**/*.test.js', '**/?(*.)+(spec|test).js'],
  collectCoverageFrom: ['**/*.js', '!**/__tests__/**', '!**/*.test.js', '!jest.config.js', '!node_modules/**', '!coverage/**'],
  coverageThreshold: { global: { statements: 90, branches: 90, functions: 90, lines: 90 } },
};
