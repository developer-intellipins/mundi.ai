// Copyright Bunting Labs, Inc. 2025

import { DriftDBProvider } from 'driftdb-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useDropzone } from 'react-dropzone';
import { useNavigate, useParams } from 'react-router-dom';
import useWebSocket from 'react-use-websocket';
import Session from 'supertokens-auth-react/recipe/session';
import MapLibreMap from './MapLibreMap';
import 'maplibre-gl/dist/maplibre-gl.css';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Map as MLMap } from 'maplibre-gl';
import { toast } from 'sonner';
import type { ErrorEntry, UploadingFile } from '../lib/frontend-types';
import type { Conversation, EphemeralAction, MapProject, MapTreeResponse } from '../lib/types';
import { usePersistedState } from '../lib/usePersistedState';

export default function ProjectView() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const sessionContext = Session.useSessionContext();

  const { projectId, versionIdParam } = useParams();
  if (!projectId) {
    throw new Error('No project ID');
  }

  // State for controlling project query refetch interval
  const [projectRefetchInterval, setProjectRefetchInterval] = useState<number | false>(false);

  // handle a single store of project<->map<->conversation data
  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => fetch(`/api/projects/${projectId}`).then((res) => res.json() as Promise<MapProject>),
    refetchInterval: projectRefetchInterval,
  });

  // Update refetch interval based on loading PostGIS connections
  useEffect(() => {
    const hasLoadingConnections = project?.postgres_connections?.some((connection) => !connection.is_documented);

    setProjectRefetchInterval(hasLoadingConnections ? 4000 : false);
  }, [project?.postgres_connections]);

  const [conversationId, setConversationId] = usePersistedState<number | null>('conversationId', [projectId], null);
  const { data: conversations } = useQuery({
    queryKey: ['project', projectId, 'conversations'],
    queryFn: () => fetch(`/api/conversations?project_id=${projectId}`).then((res) => res.json() as Promise<Conversation[]>),
  });

  const versionId = versionIdParam || (project?.maps && project.maps.length > 0 ? project.maps[project.maps.length - 1] : null);

  // When we need to trigger a refresh
  const invalidateMapData = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId, 'map', versionId] });
  }, [queryClient, projectId, versionId]);

  // Function to update project data (invalidate project queries)
  const invalidateProjectData = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId] });
  }, [queryClient, projectId]);

  const {
    isPending,
    error,
    data: mapData,
  } = useQuery({
    queryKey: ['project', projectId, 'map', versionId],
    queryFn: () => fetch(`/api/maps/${versionId}?diff_map_id=auto`).then((res) => res.json()),
    enabled: !!versionId,
  });

  const { data: mapTree } = useQuery({
    queryKey: ['project', projectId, 'map', versionId, 'tree', conversationId],
    queryFn: () =>
      fetch(`/api/maps/${versionId}/tree${conversationId ? `?conversation_id=${conversationId}` : ''}`).then(
        (res) => res.json() as Promise<MapTreeResponse>,
      ),
    enabled: !!versionId,
    placeholderData: (previousData) => {
      if (!previousData) return undefined;
      // mapTree being null/undefined makes the version visualization flicker, so
      // delete the conversation-related stuff from the tree, and use that as our
      // placeholder
      return {
        ...previousData,
        tree: previousData.tree.map((node) => ({
          ...node,
          messages: [], // conversation messages
        })),
      };
    },
  });

  const { data: roomId } = useQuery({
    queryKey: ['project', projectId, 'map', versionId, 'room'],
    queryFn: () => fetch(`/api/maps/${versionId}/room`).then((res) => res.json() as Promise<{ room_id: string }>),
    enabled: !!versionId,
  });

  // tracking ephemeral state, where reloading the page will reset
  const [errors, setErrors] = useState<ErrorEntry[]>([]);
  const [activeActions, setActiveActions] = useState<EphemeralAction[]>([]);
  const [zoomHistory, setZoomHistory] = useState<Array<{ bounds: [number, number, number, number] }>>([]);
  const [zoomHistoryIndex, setZoomHistoryIndex] = useState(-1);
  const mapRef = useRef<MLMap | null>(null);
  const processedBoundsActionIds = useRef<Set<string>>(new Set());

  // Helper function to add a new error
  const addError = useCallback((message: string, shouldOverrideMessages: boolean = false) => {
    setErrors((prevErrors) => {
      // if it already exists, bail out
      if (prevErrors.some((err) => err.message === message)) return prevErrors;

      const newError: ErrorEntry = {
        id: Date.now().toString() + Math.random().toString(36).substr(2, 9),
        message,
        timestamp: new Date(),
        shouldOverrideMessages,
      };

      console.error(message);
      if (!shouldOverrideMessages) toast.error(message);

      // schedule the auto-dismiss
      setTimeout(() => {
        setErrors((current) => current.filter((e) => e.id !== newError.id));
      }, 30000);

      return [...prevErrors, newError];
    });
  }, []);

  // Helper function to dismiss a specific error
  const dismissError = useCallback((errorId: string) => {
    setErrors((prevErrors) => prevErrors.filter((error) => error.id !== errorId));
  }, []);

  // Add state for tracking uploading files
  const [uploadingFiles, setUploadingFiles] = useState<UploadingFile[]>([]);

  // WebSocket using react-use-websocket
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const [jwt, setJwt] = useState<string | undefined>(undefined);

  // authenticating web sockets
  useEffect(() => {
    const getSessionData = async () => {
      if (await Session.doesSessionExist()) {
        const accessToken = await Session.getAccessToken();

        setJwt(accessToken);
      }
    };
    getSessionData();
  }, []);

  const wsUrl = useMemo(() => {
    if (!conversationId) {
      return null;
    } else if (!jwt) {
      return `${wsProtocol}//${window.location.host}/api/maps/ws/${conversationId}/messages/updates`;
    }

    return `${wsProtocol}//${window.location.host}/api/maps/ws/${conversationId}/messages/updates?token=${jwt}`;
  }, [conversationId, wsProtocol, jwt]);

  // Track page visibility and allow socket to remain open for 10 minutes after hidden
  const WS_REMAIN_OPEN_FOR_MS = 10 * 60 * 1000; // 10 minutes
  const [isTabVisible, setIsTabVisible] = useState<boolean>(document.visibilityState === 'visible');
  const [hiddenTimeoutExpired, setHiddenTimeoutExpired] = useState<boolean>(false);
  const hiddenTimerRef = useRef<number | null>(null);

  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        setIsTabVisible(true);
        setHiddenTimeoutExpired(false);
        if (hiddenTimerRef.current !== null) {
          clearTimeout(hiddenTimerRef.current);
          hiddenTimerRef.current = null;
        }
      } else {
        setIsTabVisible(false);
        hiddenTimerRef.current = window.setTimeout(() => {
          setHiddenTimeoutExpired(true);
          hiddenTimerRef.current = null;
        }, WS_REMAIN_OPEN_FOR_MS);
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      if (hiddenTimerRef.current !== null) {
        clearTimeout(hiddenTimerRef.current);
      }
    };
  }, []);

  // WebSocket using react-use-websocket - only connect when in a conversation
  const shouldConnect = !sessionContext.loading && conversationId !== null && (isTabVisible || !hiddenTimeoutExpired);
  const { lastMessage } = useWebSocket(
    wsUrl,
    {
      onError: () => {
        toast.error('Chat connection error.');
      },
      shouldReconnect: () => true,
      reconnectAttempts: 2880, // 24 hours of continuous work, at 30 seconds each = 2,880
      reconnectInterval: 30, // interval *between* reconnects, 30 milliseconds
    },
    shouldConnect,
  );

  // Process incoming messages
  useEffect(() => {
    if (lastMessage) {
      try {
        const update: any = JSON.parse(lastMessage.data);

        // Check if this is an ephemeral action
        if (update && typeof update === 'object' && 'ephemeral' in update && update.ephemeral === true) {
          const action = update as EphemeralAction;

          // Check if this is an error notification
          if (action.error_message) {
            // Don't add error notifications to active actions, instead treat as error
            addError(action.error_message, true);
            return; // Early return to skip normal ephemeral action handling
          }

          // Handle bounds zooming only when action becomes active (not on completion)
          if (action.bounds && action.bounds.length === 4 && mapRef.current && action.status === 'active') {
            // Check if we've already processed this action
            if (processedBoundsActionIds.current.has(action.action_id)) {
              return;
            }
            processedBoundsActionIds.current.add(action.action_id);
            // Save current bounds to history before zooming
            const currentBounds = mapRef.current.getBounds();
            const currentBoundsArray: [number, number, number, number] = [
              currentBounds.getWest(),
              currentBounds.getSouth(),
              currentBounds.getEast(),
              currentBounds.getNorth(),
            ];

            // Add both current bounds and new bounds to history in a single update
            setZoomHistory((prev) => {
              const historyUpToCurrent = prev.slice(0, zoomHistoryIndex + 1);
              return [...historyUpToCurrent, { bounds: currentBoundsArray }, { bounds: action.bounds as [number, number, number, number] }];
            });

            // Update index to point to the final new bounds (current + 2 positions)
            setZoomHistoryIndex((prev) => prev + 2);

            // Zoom to new bounds
            const [west, south, east, north] = action.bounds;
            mapRef.current.fitBounds(
              [
                [west, south],
                [east, north],
              ],
              { animate: true, padding: 50 },
            );
          }

          if (action.status === 'active') {
            // Add to active actions
            setActiveActions((prev) => [...prev, action]);
          } else if (action.status === 'completed') {
            // Remove from active actions
            setActiveActions((prev) => prev.filter((a) => a.action_id !== action.action_id));

            if (action.updates.style_json) {
              invalidateMapData();
            }
          }
        } else {
          // Non-ephemeral messages are of type SanitizedMessage
          // Regular message
          // just invalidate map data
          invalidateMapData();
        }
      } catch (e) {
        console.error('Error processing WebSocket message:', e);
        addError('Failed to process update from server.', false);
      }
    }
  }, [lastMessage, addError, zoomHistoryIndex, invalidateMapData]);

  // Helper function to upload a single file with progress tracking
  const uploadFile = useMutation({
    mutationFn: async ({ file, fileId }: { file: File; fileId: string }): Promise<{ name: string; dag_child_map_id?: string }> => {
      if (!versionId) throw new Error('No version ID available');

      const formData = new FormData();
      formData.append('file', file);

      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();

        // Track upload progress
        xhr.upload.addEventListener('progress', (event) => {
          if (event.lengthComputable) {
            const progress = Math.round((event.loaded / event.total) * 100);
            setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, progress } : f)));
          }
        });

        // Handle completion
        xhr.addEventListener('load', () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            const response = JSON.parse(xhr.responseText);
            resolve(response);
          } else {
            // Handle HTTP error status (like 400)
            let errorMessage = `Upload failed: ${xhr.statusText}`;

            // Try to parse error from response body
            try {
              const errorResponse = JSON.parse(xhr.responseText);
              if (errorResponse.detail) {
                errorMessage = errorResponse.detail;
              }
            } catch {
              // Keep the default error message if parsing fails
            }

            reject(new Error(errorMessage));
          }
        });

        // Handle network errors
        xhr.addEventListener('error', () => {
          reject(new Error('Upload failed due to network error'));
        });

        xhr.open('POST', `/api/maps/${versionId}/layers`);
        xhr.send(formData);
      });
    },
    onSuccess: (response, { fileId }) => {
      toast.success(`Layer "${response.name}" uploaded successfully! Navigating to new map...`);

      // Mark as completed
      setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, status: 'completed', progress: 100 } : f)));

      // Remove from uploading list after delay
      setTimeout(() => {
        setUploadingFiles((prev) => prev.filter((f) => f.id !== fileId));
      }, 2000);

      // Invalidate project data to refresh the project state
      queryClient.invalidateQueries({ queryKey: ['project', projectId] });

      // Navigate to the new child map if dag_child_map_id is present
      if (response.dag_child_map_id) {
        setTimeout(() => {
          navigate(`/project/${projectId}/${response.dag_child_map_id}`);
        }, 1000);
      } else {
        // Fallback: refresh the current map data
        setTimeout(() => {
          invalidateMapData();
        }, 2000);
      }
    },
    onError: (error, { file, fileId }) => {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, status: 'error', error: errorMessage } : f)));
      toast.error(`Error uploading ${file.name}: ${errorMessage}`);

      // Remove from uploading list after delay to show error state
      setTimeout(() => {
        setUploadingFiles((prev) => prev.filter((f) => f.id !== fileId));
      }, 5000);
    },
  });

  // Modified dropzone implementation to handle multiple files
  const onDrop = useCallback(
    (acceptedFiles: File[]) => {
      if (!versionId || acceptedFiles.length === 0) return;

      const maxFileSize = 100 * 1024 * 1024; // 100MB in bytes

      // Filter out files that are too large
      const validFiles = acceptedFiles.filter((file) => {
        if (file.size > maxFileSize) {
          toast.error(`File "${file.name}" is too large. Files over 100MB aren't supported yet.`);
          return false;
        }
        return true;
      });

      if (validFiles.length === 0) return;

      // Create uploading file entries
      const newUploadingFiles: UploadingFile[] = validFiles.map((file) => ({
        id: `${file.name}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
        file,
        progress: 0,
        status: 'uploading',
      }));

      // Add to uploading files state
      setUploadingFiles((prev) => [...prev, ...newUploadingFiles]);

      // Start uploading each file
      newUploadingFiles.forEach((uploadingFile) => {
        uploadFile.mutate({ file: uploadingFile.file, fileId: uploadingFile.id });
      });
    },
    [versionId, uploadFile],
  );

  const { getRootProps, getInputProps, isDragActive, open } = useDropzone({
    onDrop,
    noClick: true, // Prevent opening the file dialog when clicking
    accept: {
      'application/geo+json': ['.geojson', '.json'],
      'application/vnd.google-earth.kml+xml': ['.kml'],
      'application/vnd.google-earth.kmz': ['.kmz'],
      'image/tiff': ['.tif', '.tiff'],
      'image/jpeg': ['.jpg', '.jpeg'],
      'image/png': ['.png'],
      'application/geopackage+sqlite3': ['.gpkg'],
      'application/octet-stream': ['.fgb', '.dem'],
      'application/zip': ['.zip'],
      'application/vnd.las': ['.las'],
      'application/las+zip': ['.laz'],
    },
  });

  // Let them hide certain layers client-side only
  const [hiddenLayerIDs, setHiddenLayerIDs] = useState<string[]>([]);
  const toggleLayerVisibility = (layerId: string) => {
    setHiddenLayerIDs((prev) => (prev.includes(layerId) ? prev.filter((id) => id !== layerId) : [...prev, layerId]));
  };

  if (sessionContext.loading) {
    return <div className="p-6">Loading session...</div>;
  }

  if (!sessionContext.doesSessionExist) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">Map View</h1>
        <p>Please log in to view this map.</p>
        <a href="/auth" className="text-blue-500 hover:underline">
          Login
        </a>
      </div>
    );
  }

  if (!versionId) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">
          Loading project {projectId} version {versionId}...
        </h1>
      </div>
    );
  }

  if (isPending) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">Loading map data...</h1>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">Error Loading Map</h1>
        <p>Failed to load map data: {error.message}</p>
        <a href="/maps" className="text-blue-500 hover:underline">
          Back to Maps
        </a>
      </div>
    );
  }

  if (!mapData) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">Map Not Found</h1>
        <p>The requested map could not be found.</p>
        <a href="/maps" className="text-blue-500 hover:underline">
          Back to Maps
        </a>
      </div>
    );
  }

  return (
    <div {...getRootProps()} className={`flex grow ${isDragActive ? 'file-drag-active' : ''}`}>
      {/* Dropzone */}
      <input {...getInputProps()} />

      {/* Interactive Map Section */}
      {roomId && project ? (
        <DriftDBProvider api="/drift/" room={roomId.room_id}>
          <MapLibreMap
            mapId={versionId}
            height="100%"
            project={project}
            mapData={mapData}
            mapTree={mapTree || null}
            conversationId={conversationId}
            conversations={conversations || []}
            setConversationId={setConversationId}
            openDropzone={open}
            uploadingFiles={uploadingFiles}
            hiddenLayerIDs={hiddenLayerIDs}
            toggleLayerVisibility={toggleLayerVisibility}
            mapRef={mapRef}
            activeActions={activeActions}
            setActiveActions={setActiveActions}
            zoomHistory={zoomHistory}
            zoomHistoryIndex={zoomHistoryIndex}
            setZoomHistoryIndex={setZoomHistoryIndex}
            addError={addError}
            dismissError={dismissError}
            errors={errors}
            invalidateProjectData={invalidateProjectData}
            invalidateMapData={invalidateMapData}
          />
        </DriftDBProvider>
      ) : (
        <div className="flex items-center justify-center h-full">
          <p>Loading room information...</p>
        </div>
      )}
    </div>
  );
}
