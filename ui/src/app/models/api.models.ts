/**
 * Barrel re-export for backward compatibility.
 *
 * New code should import from the specific domain model file:
 *   - shared.models.ts     → ConnectRequest, Message, QueryResult, etc.
 *
 * This file re-exports everything so existing imports continue to work.
 */

export * from './shared.models';

