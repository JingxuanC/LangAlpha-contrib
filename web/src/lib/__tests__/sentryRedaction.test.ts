// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { redactCapabilities } from '../sentry';

describe('redactCapabilities: a path that is its own credential leaves without it', () => {
  it.each([
    ['https://app.example/s/SHARE123', 'https://app.example/s/[redacted]'],
    ['/a/SHARE123/files', '/a/[redacted]/files'],
    ['/api/v1/public/shared/SHARE123/files/read?path=x', '/api/v1/public/shared/[redacted]/files/read?path=x'],
    ['https://api.example/api/v1/wsfiles/g/GRANT1/data/a.csv', 'https://api.example/api/v1/wsfiles/g/[redacted]/data/a.csv'],
    ['/api/v1/artifacts/g/GRANT2/v/3/index.html', '/api/v1/artifacts/g/[redacted]/v/3/index.html'],
    ['/api/v1/workspaces/w1/share-links/CODE9', '/api/v1/workspaces/w1/share-links/[redacted]'],
    ['GET /api/v1/wsfiles/g/GRANT1/x failed', 'GET /api/v1/wsfiles/g/[redacted]/x failed'],
    ['Failed to load https://app.example/s/SHARE123 (404)', 'Failed to load https://app.example/s/[redacted] (404)'],
    ['from /s/A1 to "/a/B2/files"', 'from /s/[redacted] to "/a/[redacted]/files"'],
  ])('%s', (input, expected) => {
    expect(redactCapabilities(input)).toBe(expected);
  });

  it('leaves an ordinary path alone', () => {
    expect(redactCapabilities('/chat/t/abc/assets/s/x')).toBe('/chat/t/abc/assets/s/x');
    expect(redactCapabilities('/api/v1/threads/abc/feedback')).toBe('/api/v1/threads/abc/feedback');
  });
});
