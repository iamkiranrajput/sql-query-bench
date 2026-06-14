/**
 * Connection Dialog Component
 */

import { Component, Input, Output, EventEmitter, OnInit, OnDestroy, Optional } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, FormGroup, Validators, ReactiveFormsModule, FormsModule } from '@angular/forms';
import { MatDialogRef, MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { ApiService } from '../../services/api.service';
import { ComponentStateService } from '../../services/component-state.service';

@Component({
  selector: 'app-connection-dialog',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    FormsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatSelectModule,
    MatProgressSpinnerModule
  ],
  templateUrl: './connection-dialog.component.html',
  styleUrls: ['./connection-dialog.component.scss']
})
export class ConnectionDialogComponent implements OnInit, OnDestroy {
  @Input() embeddedMode = false;
  @Output() connectionSuccess = new EventEmitter<any>();

  serverForm!: FormGroup;
  databaseForm!: FormGroup;
  hidePassword = true;
  loading = false;
  error = '';
  rememberConnection = false;
  
  stage: 'server' | 'database' = 'server';
  serverConfig: any = null;
  availableDatabases: string[] = [];
  
  private readonly STATE_KEY = 'connection-dialog';
  private readonly REMEMBERED_CONNECTION_KEY = 'rememberedConnection';

  dbTypes = [
    { value: 'postgresql', label: 'PostgreSQL', defaultPort: 5432 },
    { value: 'mysql', label: 'MySQL', defaultPort: 3306 },
    { value: 'mssql', label: 'SQL Server', defaultPort: 1433 },
    { value: 'oracle', label: 'Oracle', defaultPort: 1521 }
  ];

  constructor(
    private fb: FormBuilder,
    private apiService: ApiService,
    private componentState: ComponentStateService,
    @Optional() private dialogRef?: MatDialogRef<ConnectionDialogComponent>
  ) {
    // Initialize forms in constructor to ensure they're available immediately
    this.serverForm = this.fb.group({
      dbType: ['postgresql'],
      hostname: ['localhost', Validators.required],
      port: [5432, [Validators.required, Validators.min(1), Validators.max(65535)]],
      username: ['', Validators.required],
      password: ['', Validators.required]
    });

    // Database selection form
    this.databaseForm = this.fb.group({
      database: ['', Validators.required]
    });
  }

  onDbTypeChange() {
    const dbType = this.serverForm.get('dbType')?.value;
    const selectedType = this.dbTypes.find(t => t.value === dbType);
    if (selectedType) {
      this.serverForm.patchValue({ port: selectedType.defaultPort });
    }
  }

  ngOnInit(): void {
    const saved = this.componentState.restoreState<any>(this.STATE_KEY);
    if (saved?.serverForm) {
      this.serverForm.patchValue({
        dbType: saved.serverForm.dbType || 'postgresql',
        hostname: saved.serverForm.hostname || 'localhost',
        port: saved.serverForm.port || 5432,
        username: saved.serverForm.username || ''
      });
    }
    if (saved?.stage) this.stage = saved.stage;
    if (saved?.availableDatabases) this.availableDatabases = saved.availableDatabases;
    if (saved?.serverConfig) this.serverConfig = saved.serverConfig;
    
    // Load remembered connection if exists
    this.loadRememberedConnection();
  }

  loadRememberedConnection(): void {
    const remembered = localStorage.getItem(this.REMEMBERED_CONNECTION_KEY);
    if (remembered) {
      try {
        const config = JSON.parse(remembered);
        // Decrypt password (simple base64 decode)
        if (config.encryptedPassword) {
          try {
            config.password = atob(config.encryptedPassword);
            this.rememberConnection = true;
            this.serverForm.patchValue({
              dbType: config.dbType || config.db_type || 'postgresql',
              hostname: config.hostname || '',
              port: config.port || 5432,
              username: config.username || '',
              password: config.password || ''
            });
          } catch (e) {
            console.error('Failed to decrypt remembered password:', e);
          }
        }
      } catch (e) {
        console.error('Failed to load remembered connection:', e);
      }
    }
  }

  saveRememberedConnection(config: any): void {
    if (this.rememberConnection && config.password) {
      try {
        // Encrypt password (simple base64 encode - not perfect but better than plaintext)
        const encryptedPassword = btoa(config.password);
        const configToSave = {
          dbType: config.dbType || config.db_type,
          hostname: config.hostname,
          port: config.port,
          username: config.username,
          database: config.database,
          encryptedPassword: encryptedPassword
        };
        localStorage.setItem(this.REMEMBERED_CONNECTION_KEY, JSON.stringify(configToSave));
      } catch (e) {
        console.error('Failed to save remembered connection:', e);
      }
    } else {
      // Clear remembered connection if checkbox is unchecked
      localStorage.removeItem(this.REMEMBERED_CONNECTION_KEY);
    }
  }

  ngOnDestroy(): void {
    this.componentState.saveState(this.STATE_KEY, {
      serverForm: {
        dbType: this.serverForm.value.dbType,
        hostname: this.serverForm.value.hostname,
        port: this.serverForm.value.port,
        username: this.serverForm.value.username
      },
      stage: this.stage,
      availableDatabases: this.availableDatabases,
      serverConfig: this.serverConfig
    });
  }

  // Connect to server
  onConnectToServer(): void {
    if (this.serverForm.valid) {
      this.loading = true;
      this.error = '';
      
      const config = this.serverForm.value;
      
      // List databases on the server (pass db_type)
      this.apiService.listDatabases(config.hostname, config.port, config.username, config.password, config.dbType).subscribe({
        next: (response: any) => {
          this.loading = false;
          if (response.success && response.databases.length > 0) {
            this.serverConfig = config;
            this.availableDatabases = response.databases;
            this.stage = 'database';
            
            // Auto-select database if only one exists
            if (response.databases.length === 1) {
              this.databaseForm.patchValue({ database: response.databases[0] });
              // Auto-connect after a short delay
              setTimeout(() => {
                this.onConnectToDatabase();
              }, 300);
            }
          } else {
            this.error = response.error || response.message || 'No databases found on this server';
          }
        },
        error: (err: any) => {
          this.loading = false;
          // Extract meaningful error from various response formats
          this.error = err.error?.error || err.error?.detail || err.error?.message || err.message || 'Failed to connect to server';
        }
      });
    }
  }

  // Connect to selected database
  onConnectToDatabase(): void {
    if (this.databaseForm.valid && this.serverConfig) {
      const fullConfig = {
        ...this.serverConfig,
        database: this.databaseForm.value.database
      };

      // Save remembered connection if checkbox is checked
      this.saveRememberedConnection(fullConfig);

      if (this.embeddedMode) {
        this.connectionSuccess.emit(fullConfig);
      } else if (this.dialogRef) {
        this.dialogRef.close(fullConfig);
      }
    }
  }

  // Go back to server selection
  onBackToServer(): void {
    this.stage = 'server';
    this.serverConfig = null;
    this.availableDatabases = [];
    this.error = '';
  }

  onCancel(): void {
    if (this.dialogRef) {
      this.dialogRef.close();
    }
  }
}
