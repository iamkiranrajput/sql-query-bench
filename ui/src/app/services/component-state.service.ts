import { Injectable } from '@angular/core';

@Injectable({
  providedIn: 'root'
})
export class ComponentStateService {
  private states = new Map<string, any>();

  saveState<T = any>(key: string, state: T): void {
    this.states.set(key, state);
  }

  restoreState<T = any>(key: string): T | null {
    return (this.states.get(key) as T) ?? null;
  }

  clearState(key: string): void {
    this.states.delete(key);
  }

  clearAllStates(): void {
    this.states.clear();
  }
}
