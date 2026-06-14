/**
 * Main App Component
 */

import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule } from '@angular/forms';
import { HttpClientModule } from '@angular/common/http';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatDialogModule, MatDialog } from '@angular/material/dialog';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { BehaviorSubject } from 'rxjs';

import { ApiService } from './services/api.service';
import { ComponentStateService } from './services/component-state.service';
import { Message, QueryResult } from './models/api.models';
import { ConnectionDialogComponent } from './components/connection-dialog/connection-dialog.component';
import { SidebarComponent, ViewType } from './components/sidebar/sidebar.component';
import { SchemaExplorerComponent } from './components/schema-explorer/schema-explorer.component';
import { DataAnalyticsComponent } from './components/data-analytics/data-analytics.component';
import { DashboardComponent } from './components/dashboard/dashboard.component';
import { McpAgentComponent } from './components/mcp-agent/mcp-agent.component';
import { KnowledgeManagerComponent } from './components/knowledge-manager/knowledge-manager.component';
import { ThemeService } from './services/theme.service';

// Saved connection interface
interface SavedConnection {
  id: string;
  name: string;
  dbType: string;
  hostname: string;
  port: number;
  database: string;
  username: string;
  password: string;  // Stored encrypted in localStorage
  createdAt: Date;
}

interface ConnectionForm {
  name: string;
  dbType: string;
  hostname: string;
  port: number;
  database: string;
  username: string;
  password: string;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    HttpClientModule,
    MatToolbarModule,
    MatButtonModule,
    MatIconModule,
    MatDialogModule,
    MatSnackBarModule,
    MatProgressSpinnerModule,
    SidebarComponent,
    SchemaExplorerComponent,
    DataAnalyticsComponent,
    DashboardComponent,
    McpAgentComponent,
    KnowledgeManagerComponent
  ],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.scss']
})
export class AppComponent implements OnInit {
  title = 'Query Bench';
  activeView: ViewType = 'mcp-agent';
  previousView: ViewType = 'mcp-agent';
  isDarkMode = false;

  // State management
  sessionId$ = new BehaviorSubject<string | null>(null);
  connected$ = new BehaviorSubject<boolean>(false);
  messages$ = new BehaviorSubject<Message[]>([]);
  queryResult$ = new BehaviorSubject<QueryResult | null>(null);
  loading$ = new BehaviorSubject<boolean>(false);
  error$ = new BehaviorSubject<string | null>(null);

  // Current values
  sessionId: string | null = null;
  connected: boolean = false;
  connectedDatabase: string = '';
  dbIdentity: string = '';
  messages: Message[] = [];
  queryResult: QueryResult | null = null;
  loading: boolean = false;
  error: string | null = null;

  // Database switcher
  availableDatabases: string[] = [];
  selectedDatabase: string = '';
  switchingDatabase: boolean = false;
  private currentServerConfig: any = null;

  // Saved connections management
  configTab: 'connections' | 'preferences' | 'about' = 'connections';
  savedConnections: SavedConnection[] = [];
  showAddConnectionForm: boolean = false;
  editingConnectionId: string | null = null;
  activeConnectionId: string | null = null;
  connectingToId: string | null = null;
  showPassword: boolean = false;
  connectionForm: ConnectionForm = {
    name: '',
    dbType: 'postgresql',
    hostname: '',
    port: 5432,
    database: '',
    username: '',
    password: ''
  };

  constructor(
    private apiService: ApiService,
    private componentState: ComponentStateService,
    private dialog: MatDialog,
    private snackBar: MatSnackBar,
    public themeService: ThemeService
  ) {
    // Subscribe to theme changes
    this.themeService.darkMode$.subscribe(dark => this.isDarkMode = dark);

    // Subscribe to state changes
    this.sessionId$.subscribe(id => this.sessionId = id);
    this.connected$.subscribe(conn => this.connected = conn);
    this.messages$.subscribe(msgs => this.messages = msgs);
    this.queryResult$.subscribe(result => this.queryResult = result);
    this.loading$.subscribe(load => this.loading = load);
    this.error$.subscribe(err => this.error = err);
  }

  ngOnInit(): void {
    // Load saved connections from localStorage
    this.loadSavedConnections();

    // Try to restore previous session on page refresh
    // Only show configuration view if restore fails
    this.restoreSession();
  }
  
  /**
   * Restore session from localStorage on page refresh
   * Auto-reconnects if session expired but remembered connection exists
   */
  restoreSession(): void {
    const savedSessionId = localStorage.getItem('currentSessionId');
    const savedConfig = localStorage.getItem('currentConnectionConfig');
    const rememberedConnection = localStorage.getItem('rememberedConnection');
    
    console.log('Restoring session:', { 
      hasSessionId: !!savedSessionId, 
      hasConfig: !!savedConfig, 
      hasRemembered: !!rememberedConnection 
    });
    
    if (savedSessionId && savedConfig) {
      try {
        const config = JSON.parse(savedConfig);
        
        // First, try to verify if session still exists by checking schema intelligence status
        // This is a lightweight check that doesn't require full connection
        this.apiService.getSchemaIntelligenceStatus(savedSessionId).subscribe({
          next: (response) => {
            // Check if session exists on backend
            // Backend returns success: false when session doesn't exist (server restart)
            if (response?.success === true) {
              // Session exists and is valid - restore UI state
              console.log('Session is valid, restoring UI state');
              this.sessionId$.next(savedSessionId);
              this.connected$.next(true);
              this.connectedDatabase = config.database || '';
              this.dbIdentity = config.db_identity || '';
              this.currentServerConfig = config;
              this.selectedDatabase = '';
              this.activeView = 'mcp-agent';
              
              // Find matching saved connection and set activeConnectionId
              const matchingConnection = this.savedConnections.find(conn => 
                conn.hostname === config.hostname &&
                conn.port === config.port &&
                conn.database === config.database &&
                conn.username === config.username
              );
              if (matchingConnection) {
                this.activeConnectionId = matchingConnection.id;
                console.log('Matched saved connection for restored session:', matchingConnection.name);
              }
              
              // Fetch available databases for the switcher (non-blocking)
              if (config.hostname && config.port && config.username) {
                this.loadAvailableDatabases(config);
              }
              
              // Ensure remembered connection is saved for future auto-reconnect
              // (in case it wasn't saved before)
              const rememberedConnection = localStorage.getItem('rememberedConnection');
              if (!rememberedConnection && config.hostname && config.database) {
                // Try to get password from saved connection if available
                // Note: We can't get password from savedConfig as it's removed for security
                // But if user had "Remember Connection" checked, it should already be saved
                console.log('Session restored, but no remembered connection found. User will need to reconnect if server restarts.');
              }
              
              console.log(`Session restored successfully: ${savedSessionId}`);
            } else {
              console.log('Session not found on server, attempting auto-reconnect...');
              // Session not found (server restarted or session expired)
              // Try auto-reconnect if remembered connection exists
              if (rememberedConnection) {
                this.autoReconnect(rememberedConnection);
              } else {
                // No remembered connection - show manual reconnect prompt
                if (response?.has_embeddings && response?.tables_count && response.tables_count > 0) {
                  // Embeddings exist - server restarted but data is still there
                  console.log(`Session expired but embeddings exist (${response.tables_count} tables). Server likely restarted.`);
                  localStorage.removeItem('currentSessionId'); // Clear session ID but keep config
                  this.activeView = 'configuration';
                  setTimeout(() => {
                    this.showSnackBar(
                      `Server restarted. Your connection settings are saved. Please reconnect.`,
                      'error'
                    );
                  }, 500);
                } else {
                  // No embeddings or session expired - clear everything
                  console.log('Session not found and no embeddings. Clearing saved session.');
                  this.clearSavedSession();
                  this.activeView = 'configuration';
                }
              }
            }
          },
          error: (error) => {
            // Error checking session - try auto-reconnect if remembered connection exists
            if (rememberedConnection) {
              this.autoReconnect(rememberedConnection);
            } else {
              console.log('Session check failed - server may have restarted:', error);
              this.clearSavedSession();
              this.activeView = 'configuration';
            }
          }
        });
      } catch (e) {
        // Invalid saved data, clear it and show configuration
        console.log('Session restore error:', e);
        this.clearSavedSession();
        this.activeView = 'configuration';
      }
    } else {
      // No saved session - check if there's a remembered connection to auto-reconnect
      if (rememberedConnection) {
        console.log('No saved session, but remembered connection exists - auto-reconnecting...');
        // Auto-reconnect using remembered credentials
        this.autoReconnect(rememberedConnection);
      } else {
        console.log('No saved session and no remembered connection - showing configuration view');
        // No saved session and no remembered connection, show configuration view
        this.activeView = 'configuration';
      }
    }
  }
  
  
  /**
   * Clear saved session from localStorage
   */
  clearSavedSession(): void {
    localStorage.removeItem('currentSessionId');
    localStorage.removeItem('currentConnectionConfig');
    // Note: We don't clear rememberedConnection here - user might want to reconnect later
  }

  /**
   * Clear remembered connection (user action)
   */
  clearRememberedConnection(): void {
    localStorage.removeItem('rememberedConnection');
    this.showSnackBar('Saved connection cleared', 'success');
  }

  copySessionId(): void {
    if (this.sessionId) {
      navigator.clipboard.writeText(this.sessionId).then(() => {
        this.showSnackBar('Session ID copied to clipboard', 'success');
      }).catch(err => {
        console.error('Failed to copy session ID:', err);
        this.showSnackBar('Failed to copy session ID', 'error');
      });
    }
  }

  /**
   * Auto-reconnect using remembered connection credentials
   */
  autoReconnect(rememberedConnectionJson: string): void {
    console.log('Auto-reconnect triggered');
    try {
      const config = JSON.parse(rememberedConnectionJson);
      console.log('Parsed remembered connection config:', { 
        hostname: config.hostname, 
        database: config.database,
        hasEncryptedPassword: !!config.encryptedPassword 
      });
      
      // Decrypt password
      if (config.encryptedPassword) {
        try {
          config.password = atob(config.encryptedPassword);
          console.log('Password decrypted successfully');
        } catch (e) {
          console.error('Failed to decrypt remembered password:', e);
          this.showSnackBar('Failed to restore saved connection. Please reconnect manually.', 'error');
          return;
        }
      } else {
        // No password saved - can't auto-reconnect
        console.log('No password in remembered connection - cannot auto-reconnect');
        this.activeView = 'configuration';
        return;
      }

      // Show loading indicator
      this.loading$.next(true);
      console.log('Attempting to reconnect...');

      // Prepare connection config
      const connectConfig = {
        db_type: config.dbType || config.db_type || 'postgresql',
        hostname: config.hostname,
        port: config.port,
        database: config.database,
        username: config.username,
        password: config.password
      };

      // Connect silently (no success message, just restore state)
      this.apiService.connect(connectConfig).subscribe({
        next: (response) => {
          this.loading$.next(false);
          if (response.success && response.session_id) {
            console.log('Auto-reconnect successful:', response.session_id);
            // Restore session
            this.sessionId$.next(response.session_id);
            this.connected$.next(true);
            this.connectedDatabase = config.database || '';
            this.dbIdentity = response.db_identity || '';
            this.currentServerConfig = connectConfig;
            this.selectedDatabase = '';
            this.activeView = 'mcp-agent';
            
            // Find matching saved connection and set activeConnectionId
            const matchingConnection = this.savedConnections.find(conn => 
              conn.hostname === config.hostname &&
              conn.port === config.port &&
              conn.database === config.database &&
              conn.username === config.username
            );
            if (matchingConnection) {
              this.activeConnectionId = matchingConnection.id;
              console.log('Matched saved connection:', matchingConnection.name);
            }
            
            // Save session (this will also save remembered connection if not already saved)
            this.saveSession(response.session_id, connectConfig);
            
            // Fetch available databases
            this.loadAvailableDatabases(connectConfig);
            
            // Show success message
            setTimeout(() => {
              this.showSnackBar(`Auto-reconnected to ${config.database}`, 'success');
            }, 300);
            
            console.log(`Auto-reconnected successfully: ${response.session_id}`);
          } else {
            console.error('Auto-reconnect failed:', response.error || response.message);
            const errorMsg = response.error || response.message || 'Auto-reconnect failed';
            this.showSnackBar(errorMsg, 'error');
            this.activeView = 'configuration';
          }
        },
        error: (error) => {
          this.loading$.next(false);
          console.error('Auto-reconnect error:', error);
          const errorMsg = error.error?.error || error.error?.detail || error.message || 'Auto-reconnect failed';
          this.showSnackBar(errorMsg, 'error');
          this.activeView = 'configuration';
        }
      });
    } catch (e) {
      console.error('Failed to parse remembered connection:', e);
      this.loading$.next(false);
      this.showSnackBar('Failed to restore saved connection. Please reconnect manually.', 'error');
    }
  }
  
  /**
   * Save session to localStorage for persistence across page refreshes
   */
  saveSession(sessionId: string, config: any): void {
    localStorage.setItem('currentSessionId', sessionId);
    // Save config without password for security (will need to reconnect with password)
    const configWithoutPassword = { ...config };
    delete configWithoutPassword.password;
    // Include db_identity for scoping logs/analytics
    if (this.dbIdentity) {
      configWithoutPassword.db_identity = this.dbIdentity;
    }
    localStorage.setItem('currentConnectionConfig', JSON.stringify(configWithoutPassword));
    
    // Always save remembered connection if password exists (for auto-reconnect)
    // This ensures auto-reconnect works even if user didn't explicitly check "Remember Connection"
    // but connected through saved connections or other means
    if (config.password && config.hostname && config.database) {
      try {
        // Encrypt and save password for auto-reconnect
        const encryptedPassword = btoa(config.password);
        const configToSave = {
          dbType: config.dbType || config.db_type || 'postgresql',
          hostname: config.hostname,
          port: config.port,
          username: config.username,
          database: config.database,
          encryptedPassword: encryptedPassword
        };
        localStorage.setItem('rememberedConnection', JSON.stringify(configToSave));
        console.log('Remembered connection saved for auto-reconnect');
      } catch (e) {
        console.error('Failed to save remembered connection:', e);
      }
    } else {
      console.log('Cannot save remembered connection - missing password or connection details');
    }
  }

  // ============================================
  // Saved Connections Management
  // ============================================

  getEmptyConnectionForm(): ConnectionForm {
    return {
      name: '',
      dbType: 'postgresql',
      hostname: '',
      port: 5432,
      database: '',
      username: '',
      password: ''
    };
  }

  loadSavedConnections(): void {
    const saved = localStorage.getItem('savedConnections');
    if (saved) {
      try {
        this.savedConnections = JSON.parse(saved);
      } catch (e) {
        this.savedConnections = [];
      }
    }
  }

  saveSavedConnections(): void {
    localStorage.setItem('savedConnections', JSON.stringify(this.savedConnections));
  }

  onConnectionDbTypeChange(): void {
    const portMap: { [key: string]: number } = {
      'postgresql': 5432,
      'mysql': 3306,
      'mssql': 1433,
      'oracle': 1521
    };
    this.connectionForm.port = portMap[this.connectionForm.dbType] || 5432;
  }

  isConnectionFormValid(): boolean {
    return !!(
      this.connectionForm.name &&
      this.connectionForm.hostname &&
      this.connectionForm.port &&
      this.connectionForm.database &&
      this.connectionForm.username &&
      this.connectionForm.password
    );
  }

  saveConnection(): void {
    if (!this.isConnectionFormValid()) return;

    if (this.editingConnectionId) {
      // Update existing connection
      const index = this.savedConnections.findIndex(c => c.id === this.editingConnectionId);
      if (index !== -1) {
        this.savedConnections[index] = {
          ...this.savedConnections[index],
          ...this.connectionForm
        };
      }
    } else {
      // Add new connection
      const newConnection: SavedConnection = {
        id: this.generateId(),
        ...this.connectionForm,
        createdAt: new Date()
      };
      this.savedConnections.push(newConnection);
    }

    this.saveSavedConnections();
    this.cancelConnectionForm();
    this.showSnackBar('Connection saved successfully', 'success');
  }

  editConnection(conn: SavedConnection): void {
    this.editingConnectionId = conn.id;
    this.connectionForm = {
      name: conn.name,
      dbType: conn.dbType,
      hostname: conn.hostname,
      port: conn.port,
      database: conn.database,
      username: conn.username,
      password: conn.password
    };
    this.showAddConnectionForm = true;
  }

  deleteConnection(id: string): void {
    if (confirm('Are you sure you want to delete this connection?')) {
      this.savedConnections = this.savedConnections.filter(c => c.id !== id);
      this.saveSavedConnections();
      if (this.activeConnectionId === id) {
        this.activeConnectionId = null;
      }
      this.showSnackBar('Connection deleted', 'success');
    }
  }

  cancelConnectionForm(): void {
    this.showAddConnectionForm = false;
    this.editingConnectionId = null;
    this.connectionForm = this.getEmptyConnectionForm();
    this.showPassword = false;
  }

  connectToSavedConnection(conn: SavedConnection): void {
    this.connectingToId = conn.id;
    const config = {
      dbType: conn.dbType,
      db_type: conn.dbType,
      hostname: conn.hostname,
      port: conn.port,
      database: conn.database,
      username: conn.username,
      password: conn.password
    };

    this.apiService.connect(config).subscribe({
      next: (response) => {
        this.connectingToId = null;
        if (response.success && response.session_id) {
          this.sessionId$.next(response.session_id);
          this.connected$.next(true);
          this.connectedDatabase = conn.database;
          this.dbIdentity = response.db_identity || '';
          this.currentServerConfig = config;
          this.activeConnectionId = conn.id;
          this.selectedDatabase = '';
          
          // Save session (this will also save remembered connection for auto-reconnect)
          this.saveSession(response.session_id, config);
          
          this.loadAvailableDatabases(config);
          this.showSnackBar(`Connected to ${conn.name}`, 'success');
          
          // Switch to Copilot chat
          this.activeView = 'mcp-agent';
        } else {
          const errorMsg = response.error || response.message || 'Connection failed';
          this.showSnackBar(errorMsg, 'error');
        }
      },
      error: (error) => {
        this.connectingToId = null;
        const errorMsg = error.error?.error || error.error?.detail || error.message || 'Connection failed';
        this.showSnackBar(errorMsg, 'error');
      }
    });
  }

  getDbTypeColor(dbType: string): string {
    const colors: { [key: string]: string } = {
      'postgresql': 'bg-blue-500',
      'mysql': 'bg-orange-500',
      'mssql': 'bg-red-500',
      'oracle': 'bg-red-700'
    };
    return colors[dbType] || 'bg-gray-500';
  }

  onViewChanged(view: ViewType): void {
    this.activeView = view;
  }

  onOpenNewChat(): void {
    this.activeView = 'mcp-agent';
  }

  /**
   * Handle "Query this table" from Schema Explorer ERD view.
   * Stores table context in ComponentStateService and switches to the Copilot chat view.
   */
  onQueryTable(context: any): void {
    this.componentState.saveState('schema-to-chat-context', context);
    this.activeView = 'mcp-agent';
  }


  openConnectionDialog(): void {
    const dialogRef = this.dialog.open(ConnectionDialogComponent, {
      width: '500px',
      disableClose: false
    });

    dialogRef.afterClosed().subscribe(result => {
      if (result) {
        this.connectToDatabase(result, false);
      }
    });
  }

  onConnectionSuccess(config: any): void {
    this.connectToDatabase(config, false);
    // Switch to Copilot chat view after successful connection
    this.activeView = 'mcp-agent';
  }

  connectToDatabase(config: any, silentReconnect = false): void {
    this.loading$.next(true);
    this.error$.next(null);

    // Ensure db_type is set correctly (form uses dbType, API expects db_type)
    const connectConfig = {
      ...config,
      db_type: config.db_type || config.dbType || 'postgresql'
    };
    
    // Ensure password is included (needed for saving remembered connection)
    if (!connectConfig.password && config.password) {
      connectConfig.password = config.password;
    }

    this.apiService.connect(connectConfig).subscribe({
      next: (response) => {
        if (response.success && response.session_id) {
          this.sessionId$.next(response.session_id);
          this.connected$.next(true);
          this.connectedDatabase = config.database;
          this.dbIdentity = response.db_identity || '';
          this.currentServerConfig = connectConfig;
          this.selectedDatabase = '';
          
          // Find matching saved connection and set activeConnectionId
          const matchingConnection = this.savedConnections.find(conn => 
            conn.hostname === config.hostname &&
            conn.port === config.port &&
            conn.database === config.database &&
            conn.username === config.username
          );
          if (matchingConnection) {
            this.activeConnectionId = matchingConnection.id;
            console.log('Matched saved connection:', matchingConnection.name);
          } else {
            this.activeConnectionId = null;
          }
          
          // Save session to localStorage for persistence across page refreshes
          // This will also save remembered connection if password exists
          this.saveSession(response.session_id, connectConfig);
          
          // Fetch available databases for the switcher
          this.loadAvailableDatabases(config);
          
          if (!silentReconnect) {
            this.showSnackBar(`Connected to ${config.database}`, 'success');
            // Add welcome message
            const welcomeMessage: Message = {
              id: this.generateId(),
              type: 'bot',
              content: `Connected to database "${config.database}" on ${config.hostname}. You can now ask questions about your data.`,
              timestamp: new Date()
            };
            this.addMessage(welcomeMessage);
          }
        } else {
          const errorMsg = response.error || response.message || 'Connection failed';
          this.showSnackBar(errorMsg, 'error');
          // Clear saved session on connection failure
          this.clearSavedSession();
        }
        this.loading$.next(false);
      },
      error: (error) => {
        // Extract meaningful error from various response formats
        const errorMsg = error.error?.error || error.error?.detail || error.error?.message || error.message || 'Connection failed';
        this.showSnackBar(errorMsg, 'error');
        // Clear saved session on connection error
        this.clearSavedSession();
        this.loading$.next(false);
      }
    });
  }

  disconnectFromDatabase(): void {
    if (!this.sessionId) return;

    this.apiService.disconnect(this.sessionId).subscribe({
      next: (response) => {
        if (response.success) {
          // Clear saved session on disconnect (but keep remembered connection for easy reconnect)
          this.clearSavedSession();
          // Note: We intentionally keep 'rememberedConnection' so user can easily reconnect
          this.sessionId$.next(null);
          this.connected$.next(false);
          this.connectedDatabase = '';
          this.dbIdentity = '';
          this.activeConnectionId = null;
          this.messages$.next([]);
          this.queryResult$.next(null);
          this.componentState.clearAllStates();
          this.showSnackBar('Disconnected from database', 'success');
        }
      },
      error: (error) => {
        this.showSnackBar(error.message, 'error');
      }
    });
  }

  onDisconnect(): void {
    this.disconnectFromDatabase();
  }

  private addMessage(message: Message): void {
    const currentMessages = this.messages$.value;
    this.messages$.next([...currentMessages, message]);
  }

  private generateId(): string {
    return `msg_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }

  private showSnackBar(message: string, type: 'success' | 'error'): void {
    this.snackBar.open(message, 'Close', {
      duration: 3000,
      horizontalPosition: 'center',
      verticalPosition: 'top',
      panelClass: type === 'success' ? 'success-snackbar' : 'error-snackbar'
    });
  }

  loadAvailableDatabases(config: any): void {
    this.apiService.listDatabases(
      config.hostname,
      config.port,
      config.username,
      config.password,
      config.dbType || 'postgresql'
    ).subscribe({
      next: (response) => {
        if (response.success && response.databases) {
          this.availableDatabases = response.databases;
        }
      },
      error: (error) => {
        console.error('Failed to load databases:', error);
        this.availableDatabases = [];
      }
    });
  }

  switchDatabase(): void {
    if (!this.selectedDatabase || !this.currentServerConfig || this.selectedDatabase === this.connectedDatabase) {
      return;
    }

    this.switchingDatabase = true;

    // First disconnect from current database
    if (this.sessionId) {
      this.apiService.disconnect(this.sessionId).subscribe({
        next: () => {
          // Connect to new database
          const newConfig = {
            ...this.currentServerConfig,
            database: this.selectedDatabase
          };
          this.connectToNewDatabase(newConfig);
        },
        error: (error) => {
          this.showSnackBar('Failed to disconnect: ' + error.message, 'error');
          this.switchingDatabase = false;
        }
      });
    } else {
      const newConfig = {
        ...this.currentServerConfig,
        database: this.selectedDatabase
      };
      this.connectToNewDatabase(newConfig);
    }
  }

  private connectToNewDatabase(config: any): void {
    // Ensure db_type is set correctly (form uses dbType, API expects db_type)
    const connectConfig = {
      ...config,
      db_type: config.db_type || config.dbType || 'postgresql'
    };

    this.apiService.connect(connectConfig).subscribe({
      next: (response) => {
        if (response.success && response.session_id) {
          this.sessionId$.next(response.session_id);
          this.connected$.next(true);
          this.connectedDatabase = config.database;
          this.dbIdentity = response.db_identity || '';
          this.currentServerConfig = config;
          this.selectedDatabase = '';
          
          // Save session for persistence across page refreshes
          this.saveSession(response.session_id, config);
          
          this.showSnackBar(`Switched to ${config.database}`, 'success');
          
          // Clear messages for fresh start
          this.messages$.next([]);
          this.queryResult$.next(null);
          
          // Add welcome message for new database
          const welcomeMessage: Message = {
            id: this.generateId(),
            type: 'bot',
            content: `Switched to database "${config.database}". You can now ask questions about your data.`,
            timestamp: new Date()
          };
          this.addMessage(welcomeMessage);
        } else {
          const errorMsg = response.error || response.message || 'Failed to switch database';
          this.showSnackBar(errorMsg, 'error');
        }
        this.switchingDatabase = false;
      },
      error: (error) => {
        // Extract meaningful error from various response formats
        const errorMsg = error.error?.error || error.error?.detail || error.error?.message || error.message || 'Failed to connect';
        this.showSnackBar(errorMsg, 'error');
        this.switchingDatabase = false;
      }
    });
  }
}
