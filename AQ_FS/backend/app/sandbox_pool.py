"""
Pre-provisions Daytona sandboxes so users never wait for npm install.
On server startup, creates a pool of ready-to-use sandboxes.
When one is consumed, a new one is provisioned in the background.
"""
import asyncio
from collections import deque


class SandboxPool:
    def __init__(self, pool_size=2):
        self.pool_size = pool_size
        self.available: deque = deque()
        self.lock = asyncio.Lock()
        self._provisioning = False
        self._initialized = False

    async def initialize(self):
        """Call on server startup. Pre-provisions sandboxes in parallel."""
        if self._initialized:
            print("[sandbox-pool] Already initialized, skipping")
            return
        self._initialized = True
        print(f"[sandbox-pool] Pre-warming {self.pool_size} sandboxes...")
        tasks = [self._provision_one() for _ in range(self.pool_size)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and "sandbox_id" in r:
                self.available.append(r)
                print(f"[sandbox-pool] Pre-warmed sandbox {r['sandbox_id'][:12]}")
            else:
                print(f"[sandbox-pool] Failed to pre-warm sandbox: {r}")
        print(f"[sandbox-pool] Pool ready: {len(self.available)} available")

    async def acquire(self, progress=None) -> dict:
        """
        Get a pre-provisioned sandbox. Returns immediately if pool has one.
        Falls back to on-demand provisioning if pool is empty.
        Kicks off background replenishment after acquiring.
        """
        async with self.lock:
            if self.available:
                sandbox = self.available.popleft()
                print(f"[sandbox-pool] Acquired sandbox {sandbox['sandbox_id'][:12]} "
                      f"({len(self.available)} remaining)")
                # Replenish in background
                asyncio.create_task(self._replenish())
                return sandbox

        # Pool empty — provision on demand
        print("[sandbox-pool] Pool empty — provisioning on demand (slow path)")
        return await self._provision_one(progress=progress)

    async def _replenish(self):
        """Add one sandbox back to the pool in the background."""
        async with self.lock:
            if len(self.available) >= self.pool_size:
                return  # Pool already full
            if self._provisioning:
                return  # Already provisioning
            self._provisioning = True

        try:
            sandbox = await self._provision_one()
            async with self.lock:
                if len(self.available) < self.pool_size:
                    self.available.append(sandbox)
                    print(f"[sandbox-pool] Replenished to {len(self.available)} sandboxes")
        except Exception as e:
            print(f"[sandbox-pool] Replenish failed: {e}")
        finally:
            async with self.lock:
                self._provisioning = False

    async def _provision_one(self, progress=None) -> dict:
        from app.sandbox import create_react_boilerplate_sandbox
        return await create_react_boilerplate_sandbox(progress=progress)

    @property
    def size(self) -> int:
        return len(self.available)


# Global singleton
sandbox_pool = SandboxPool(pool_size=1)
