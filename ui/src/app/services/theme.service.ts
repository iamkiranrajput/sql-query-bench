import { Injectable } from '@angular/core';
import { BehaviorSubject } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class ThemeService {
  private readonly STORAGE_KEY = 'app-dark-mode';
  private darkModeSubject = new BehaviorSubject<boolean>(false);

  /** Observable for dark mode state */
  darkMode$ = this.darkModeSubject.asObservable();

  /** Current dark mode value */
  get isDarkMode(): boolean {
    return this.darkModeSubject.value;
  }

  constructor() {
    const saved = localStorage.getItem(this.STORAGE_KEY);
    if (saved === 'true') {
      this.applyDarkMode(true);
    }
  }

  toggleDarkMode(): void {
    this.applyDarkMode(!this.isDarkMode);
  }

  setDarkMode(value: boolean): void {
    this.applyDarkMode(value);
  }

  private applyDarkMode(value: boolean): void {
    this.darkModeSubject.next(value);
    localStorage.setItem(this.STORAGE_KEY, String(value));
    document.body.classList.toggle('dark-theme', value);
  }
}
