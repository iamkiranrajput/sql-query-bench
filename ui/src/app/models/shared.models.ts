/**
 * Shared TypeScript interfaces.
 *
 * Architectural boundary:
 *   shared.models   → Connection, health, base message types  (SHARED)
 *   chat.models      → Chat session, message, history           (CHAT-ONLY)
 */

// ============================================================================
// Connection / Disconnect (shared by both features)
// ============================================================================

export interface ConnectRequest {
  hostname: string;
  port: number;
  database: string;
  username: string;
  password: string;
  db_type?: string;  // postgresql, mysql, mssql, oracle
}

export interface ConnectResponse {
  success: boolean;
  session_id?: string;
  db_identity?: string;
  message: string;
  error?: string;
}

export interface DisconnectRequest {
  session_id: string;
}

export interface DisconnectResponse {
  success: boolean;
  message: string;
  error?: string;
}

// ============================================================================
// Health (shared)
// ============================================================================

export interface HealthResponse {
  status: string;
  version: string;
  timestamp: string;
  active_sessions: number;
}

// ============================================================================
// Base UI types (shared)
// ============================================================================

export interface Message {
  id: string;
  type: 'user' | 'bot';
  content: string;
  timestamp: Date;
  sqlQuery?: string;
  error?: boolean;
}

export interface QueryResult {
  records: Record<string, any>[];
  csvData?: string;
  rowCount: number;
  executionTime: number;
}

// ============================================================================
// LLM Configuration (shared)
// ============================================================================

export interface LLMConfigureRequest {
  endpoint: string;
  api_key: string;
  api_version?: string;
  user_id?: string;
  oauth_client_id?: string;
  oauth_client_secret?: string;
  oauth_token_url?: string;
}

export interface LLMConfigureResponse {
  success: boolean;
  message: string;
  models: string[];
  current_model?: string;
}

export interface LLMModelListResponse {
  success: boolean;
  models: string[];
  current_model?: string;
  endpoint?: string;
}

export interface LLMSwitchModelRequest {
  model: string;
}

export interface LLMSwitchModelResponse {
  success: boolean;
  model: string;
  message: string;
}

export interface LLMStatusResponse {
  success: boolean;
  configured: boolean;
  endpoint?: string;
  current_model?: string;
  is_gemini: boolean;
  has_oauth: boolean;
}
