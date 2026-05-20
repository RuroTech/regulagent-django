"""
Step-by-Step Automation Controller

This module provides keyboard-driven step-by-step automation control,
allowing users to progress through automation steps by pressing 'K'.
"""

import sys
import tty
import termios
import asyncio
from typing import Optional, Callable
from datetime import datetime

class StepByStepController:
    """Controls step-by-step automation execution with keyboard input."""
    
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.step_count = 0
        self.start_time = datetime.now()
        
    def _get_single_char(self) -> str:
        """Get a single character from stdin without pressing Enter."""
        if not self.enabled:
            return 'k'  # Auto-continue if disabled
            
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            char = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return char.lower()
    
    def wait_for_key(self, step_description: str, details: str = "", next_action: str = "") -> bool:
        """
        Wait for user to press 'K' to continue to the next step.
        
        Args:
            step_description: Brief description of current step
            details: Detailed explanation of what's happening
            next_action: What will happen when user presses K
            
        Returns:
            True to continue, False to abort
        """
        if not self.enabled:
            return True
            
        self.step_count += 1
        elapsed = (datetime.now() - self.start_time).total_seconds()
        
        print("\n" + "="*80)
        print(f"🎯 STEP {self.step_count}: {step_description}")
        print(f"⏱️  Elapsed: {elapsed:.1f}s")
        print("="*80)
        
        if details:
            print(f"📋 CURRENT STATUS:")
            print(f"   {details}")
            print()
        
        if next_action:
            print(f"➡️  NEXT ACTION:")
            print(f"   {next_action}")
            print()
        
        print("🎮 CONTROLS:")
        print("   Press 'K' to continue to next step")
        print("   Press 'Q' to quit automation")
        print("   Press 'S' to skip step-by-step mode (auto-continue)")
        print()
        
        while True:
            print("⌨️  Waiting for input... ", end="", flush=True)
            char = self._get_single_char()
            print(f"[{char.upper()}]")
            
            if char == 'k':
                print("✅ Continuing to next step...\n")
                return True
            elif char == 'q':
                print("🛑 User requested quit")
                return False
            elif char == 's':
                print("⏩ Skipping step-by-step mode - automation will continue automatically")
                self.enabled = False
                return True
            else:
                print(f"❌ Invalid key '{char.upper()}'. Use K (continue), Q (quit), or S (skip mode)")
    
    def log_action(self, action: str, success: bool = True, details: str = ""):
        """Log an action that was just completed."""
        if not self.enabled:
            return
            
        status = "✅" if success else "❌"
        print(f"{status} {action}")
        if details:
            print(f"   {details}")
    
    def log_info(self, message: str):
        """Log informational message."""
        if not self.enabled:
            return
        print(f"ℹ️  {message}")
    
    def log_warning(self, message: str):
        """Log warning message."""
        if not self.enabled:
            return
        print(f"⚠️  {message}")
    
    def log_error(self, message: str):
        """Log error message."""
        if not self.enabled:
            return
        print(f"❌ {message}")


class StepByStepMixin:
    """Mixin to add step-by-step functionality to automation classes."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.step_controller: Optional[StepByStepController] = None
    
    def enable_step_by_step(self, enabled: bool = True):
        """Enable or disable step-by-step mode."""
        self.step_controller = StepByStepController(enabled) if enabled else None
    
    def wait_for_step(self, step_description: str, details: str = "", next_action: str = "") -> bool:
        """Wait for user input before proceeding to next step."""
        if self.step_controller:
            return self.step_controller.wait_for_key(step_description, details, next_action)
        return True
    
    def log_step_action(self, action: str, success: bool = True, details: str = ""):
        """Log a completed action."""
        if self.step_controller:
            self.step_controller.log_action(action, success, details)
    
    def log_step_info(self, message: str):
        """Log informational message."""
        if self.step_controller:
            self.step_controller.log_info(message)
    
    def log_step_warning(self, message: str):
        """Log warning message."""
        if self.step_controller:
            self.step_controller.log_warning(message)
    
    def log_step_error(self, message: str):
        """Log error message."""
        if self.step_controller:
            self.step_controller.log_error(message)
