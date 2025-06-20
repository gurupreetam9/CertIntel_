
'use client';

import type { User } from 'firebase/auth';
import { createContext, useState, useEffect, type ReactNode, useCallback } from 'react';
import { onAuthStateChanged as firebaseOnAuthStateChanged } from '@/lib/firebase/auth'; // Renamed to avoid conflict
import { firestore } from '@/lib/firebase/config'; // Direct import for firestore
import { doc, onSnapshot, type Unsubscribe } from 'firebase/firestore'; // Import onSnapshot and Unsubscribe
import type { UserProfile } from '@/lib/models/user';
import { Loader2 } from 'lucide-react';

interface AuthContextType {
  user: User | null;
  userProfile: UserProfile | null;
  loading: boolean;
  userId: string | null;
  refreshUserProfile: () => void; // Kept for potential explicit refreshes
}

export const AuthContext = createContext<AuthContextType>({
  user: null,
  userProfile: null,
  loading: true,
  userId: null,
  refreshUserProfile: () => {},
});

const USERS_COLLECTION = 'users';

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [userProfile, setUserProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [userId, setUserId] = useState<string | null>(null);
  
  // profileRefreshKey and its related effect are removed as onSnapshot handles real-time updates.
  // The refreshUserProfile function is kept but might be a no-op or re-evaluated later if specific manual refresh logic is needed.
  const refreshUserProfile = useCallback(() => {
    console.log("AuthContext: refreshUserProfile called. (Currently a no-op due to real-time listener, but can be enhanced if needed).");
    // If a manual re-fetch bypassing the listener cache is ever needed, logic could go here.
    // For now, the listener should keep things up-to-date.
  }, []);

  useEffect(() => {
    setLoading(true);
    let profileListenerUnsubscribe: Unsubscribe | undefined = undefined;

    console.log("AuthContext: Setting up onAuthStateChanged listener.");
    const authUnsubscribe = firebaseOnAuthStateChanged(async (firebaseUser) => {
      console.log("AuthContext: onAuthStateChanged triggered. Firebase user UID:", firebaseUser ? firebaseUser.uid : 'null');
      
      // Clean up previous profile listener before setting new user or new listener
      if (profileListenerUnsubscribe) {
        console.log("AuthContext: Unsubscribing from previous profile listener for UID:", user?.uid);
        profileListenerUnsubscribe();
        profileListenerUnsubscribe = undefined;
      }
      
      setUser(firebaseUser); // Set Firebase user
      setUserId(firebaseUser ? firebaseUser.uid : null); // Set userId
      setUserProfile(null); // Reset profile initially on auth change

      if (firebaseUser) {
        setLoading(true); // Set loading true before attaching new listener
        const userDocRef = doc(firestore, USERS_COLLECTION, firebaseUser.uid);
        console.log("AuthContext: Subscribing to profile snapshots for UID:", firebaseUser.uid);

        profileListenerUnsubscribe = onSnapshot(userDocRef, 
          (docSnap) => {
            if (docSnap.exists()) {
              const newProfile = docSnap.data() as UserProfile;
              setUserProfile(newProfile);
              console.log("AuthContext: User profile updated from snapshot for UID:", firebaseUser.uid, newProfile);
            } else {
              setUserProfile(null);
              console.warn("AuthContext: User profile NOT FOUND in Firestore (onSnapshot for UID:", firebaseUser.uid, ")");
            }
            setLoading(false); // Profile data (or lack thereof) received
          },
          (error) => {
            console.error("AuthContext: Error listening to user profile (onSnapshot) for UID:", firebaseUser.uid, error);
            setUserProfile(null);
            setLoading(false);
          }
        );
      } else {
        // No user, so not loading, ensure profile is null
        setUserProfile(null);
        setLoading(false);
        console.log("AuthContext: No Firebase user. Loading false, profile null.");
      }
    });

    return () => {
      console.log("AuthContext: Unsubscribing from onAuthStateChanged and any active profile listener.");
      authUnsubscribe();
      if (profileListenerUnsubscribe) {
        profileListenerUnsubscribe();
      }
    };
  }, []); // Empty dependency array: runs once on mount, cleans up on unmount

  // This global loader is for the very initial app shell loading.
  if (loading && user === undefined) { // More specific condition: only show global loader if user state is truly unknown initially
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <Loader2 className="h-16 w-16 animate-spin text-primary" />
        <p className="ml-4 text-muted-foreground">Initializing App & Checking Authentication...</p>
      </div>
    );
  }

  return (
    <AuthContext.Provider value={{ user, userProfile, loading, userId, refreshUserProfile }}>
      {children}
    </AuthContext.Provider>
  );
};
