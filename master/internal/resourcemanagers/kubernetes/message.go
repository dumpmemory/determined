package kubernetes

import (
	"github.com/determined-ai/determined/master/pkg/actor"
	"github.com/determined-ai/determined/master/pkg/cproto"
	"github.com/determined-ai/determined/master/pkg/logger"
	"github.com/determined-ai/determined/master/pkg/tasks"
)

// Incoming pods actor messages; pods actors must accept these messages.
type (
	// StartTaskPod notifies the pods actor to start a pod with the task spec.
	StartTaskPod struct {
		TaskActor *actor.Ref
		Spec      tasks.TaskSpec
		Slots     int
		Rank      int

		LogContext logger.Context
	}
	// KillTaskPod notifies the pods actor to kill a pod.
	KillTaskPod struct {
		PodID cproto.ID
	}

	// PreemptTaskPod notifies the pods actor to preempt a pod.
	PreemptTaskPod struct {
		PodName string
	}

	// ChangePriority notifies the pods actor of a priority change and to preempt the specified pod.
	ChangePriority struct {
		PodID cproto.ID
	}

	// SetPodOrder notifies the pods actor to set the queue position of a pod.
	SetPodOrder struct {
		QPosition float64
		PodID     cproto.ID
	}
)
