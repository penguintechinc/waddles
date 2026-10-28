import '@testing-library/jest-dom';
import { configure } from '@testing-library/react';
import { vi } from 'vitest';

// The suite runs several times slower under v8 coverage with a full worker
// pool (and on a busy shared runner). RTL's default 1s async-util timeout and
// vitest's 5s test timeout then make multi-step interaction tests flake.
// Widen both -- a passing assertion still returns immediately, so this only
// changes how long a genuinely failing test waits before it reports.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20000 });
