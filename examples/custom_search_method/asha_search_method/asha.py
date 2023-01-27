"""
This example shows ASHA (Asynchronous Successive Halving Algorithm) implemented as a custom SearchMethod.
For details related to ASHA see https://docs.determined.ai/latest/training/hyperparameter/search-methods/hp-adaptive-asha.html

ASHASearchMethod provides implementation for abstract methods from SearchMethod class, which
are invoked by SearchRunner in response to the SearcherEvents received
from the multi-trial experiment. The methods return a list of Operations to SearchRunner
which sends them to the multi-trial experiment for execution, as depicted below.

Multi-trial experiment  --- (SearcherEvent1) ----> SearchRunner  --- (SearcherEvent1) ---> SearchMethod
Multi-trial experiment <---   (Operations1)  ----  SearchRunner <---   (Operations1)  ---  SearchMethod
Multi-trial experiment  --- (SearcherEvent2) ----> SearchRunner  --- (SearcherEvent2) ---> SearchMethod
Multi-trial experiment <---   (Operations2)  ----  SearchRunner <---   (Operations2)  ---  SearchMethod
and so on.

Currently, we support the following operations:
  -> Create - starts a new trial with a unique trial id and a set of hyperparameters,
  -> ValidateAfter - sets number of steps (i.e., batches or epochs) after which a validation is run for a trial
                     with a given id,
  -> Close - closes a trial with a given id,
  -> Shutdown - closes the experiment.

To support fault tolerance, a custom SearchMethod has to implement save_method_state() and load_method_state(), which
provide logic for saving and loading any information internal to the SearchMethod,
such as variables, structures, or models, that are modified as the SearchMethod progresses.
save_method_state() and load_method_state() are called by SearchRunner as a part of SearchRunner.save() and
SearchRunner.load() to ensure that SearchRunner state and SearchMethod state can be restored if
SearchRunner is terminated or interrupted.

While implementation of save_method_state() and load_method_state() depends on the user,
in this example we chose to encapsulate all variables required by ASHASearchMethod in a new class,
ASHASearchMethodState. We elected to use pickle as the storage format for convenience.
"""

import dataclasses
import logging
import pickle
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Set

from determined import searcher


class ASHASearchMethod(searcher.SearchMethod):
    def __init__(
        self,
        search_space: Callable[[], Dict[str, object]],
        max_length: int,
        max_trials: int,
        num_rungs: int,
        divisor: int,
        max_concurrent_trials: int = 0,
    ) -> None:
        # Store all information about ASHASearchMethod in ASHASearchMethodState
        # to support easy saving and loading
        self.asha_search_state = ASHASearchMethodState(
            max_length, max_trials, num_rungs, divisor, max_concurrent_trials
        )

        # Function defining hyperparameter search space.
        self.search_space = search_space

    ############################################################################
    # Invoked only once, when starting a new experiment. Creates initial list
    # of operations.
    # In this example, we create and submit operations for first N trials, such that:
    #   1) each trial is assigned a unique request_id and every operation
    #      contains request_id of a trial it refers to;
    #   2) each trial is initialized with two operations:
    #      -> "Create" operation that takes in trial's request_id and hyperparameters;
    #         in this example hyperparamters are generated by user-defined method
    #         search_space(),
    #      -> "ValidateAfter" operation that takes in trial's request_id and number of
    #         units (batches or epochs) that the model is trained for before validation;
    #         units selection is made in the custom_config.yaml.
    #
    # Note: the order in which trials are created is not guaranteed.
    def initial_operations(self, _: searcher.SearcherState) -> List[searcher.Operation]:
        ops: List[searcher.Operation] = []
        N = self._get_max_concurrent_trials()

        for __ in range(0, N):
            create = searcher.Create(
                request_id=uuid.uuid4(),
                hparams=self.search_space(),
                checkpoint=None,
            )
            ops.append(create)
            ops.append(
                searcher.ValidateAfter(
                    request_id=create.request_id,
                    length=self.asha_search_state.rungs[0].units_needed,
                )
            )

            self.asha_search_state.trial_rungs[create.request_id] = 0
            self.asha_search_state.pending_trials += 1

        return ops

    ############################################################################
    # Invoked when a trial with specific request_id is created.
    # In this example, ASHASearchMethodState is updated with
    # information about a trial's progress and no new operations are created.
    def on_trial_created(
        self, _: searcher.SearcherState, request_id: uuid.UUID
    ) -> List[searcher.Operation]:
        self.asha_search_state.rungs[0].outstanding_trials += 1
        self.asha_search_state.trial_rungs[request_id] = 0
        return []

    ############################################################################
    # Invoked when a validation for a trial with specific request_id is completed.
    # Provides a result of validation according to the metric specified in the
    # custom_config.yaml: validation_loss.
    # In this example,ASHASearchMethodState statistics are updated,
    # and based on the current number of trials and their metric values,
    # ASHA decides whether to:
    #    (1) promote a trial, i.e., to continue training for the trial
    #    (2) start a new trial,
    #    (3) close experiment if all trials are completed and maximum number of
    #        trials is reached.
    def on_validation_completed(
        self, _: searcher.SearcherState, request_id: uuid.UUID, metric: Any, train_length: int
    ) -> List[searcher.Operation]:
        assert isinstance(metric, float)
        self.asha_search_state.pending_trials -= 1
        if self.asha_search_state.is_smaller_better is False:
            metric *= -1
        ops = self._promote_async(request_id, metric)
        return ops

    ############################################################################
    # Invoked when a trial with specific request_id is closed.
    # In this example, first, ASHASearchMethodState statistics are updated.
    # Next, if all requested trials are completed and the number of
    # maximum trials has been reached, Shutdown operation is sent to close
    # the experiment. Note, that Shutdown operation does not take request_id
    # as input, since it refers to the experiment, not a single trial.
    def on_trial_closed(
        self, _: searcher.SearcherState, request_id: uuid.UUID
    ) -> List[searcher.Operation]:
        self.asha_search_state.completed_trials += 1
        self.asha_search_state.closed_trials.add(request_id)

        if (
            self.asha_search_state.pending_trials == 0
            and self.asha_search_state.completed_trials == self.asha_search_state.max_trials
        ):
            return [searcher.Shutdown()]

        return []

    ############################################################################
    # Invoked when a trial is interrupted prematurely. The possible ExitedReason
    # are ERRORED, USER_CANCELLED and INVALID_HP. While ExitedReason.INVALID_HP
    # is provided for consistency, note that there is no need to raise INVALID_HP
    # for hyperparameters you do not wish to train and test your model for.
    # Instead, just make sure that your search method is not producing
    # the undesirable hyperparameters.
    # In this example, first ASHASearchMethodState statistics are updated.
    # Next, ASHA invoked _promote_async function to decided whether to promote
    # existing trial, start a new trial or close the experiment.
    def on_trial_exited_early(
        self,
        _: searcher.SearcherState,
        request_id: uuid.UUID,
        exited_reason: searcher.ExitedReason,
    ) -> List[searcher.Operation]:
        self.asha_search_state.pending_trials -= 1

        # The "if" statement below can be completely removed.
        # There is no need to raise INVALID_HP for hyperparameters
        # you do not wish to train and test your model for.
        # Instead, make sure to start trials only with hyperparameters
        # that satisfy your criteria. For instance, in this example,
        # sample_params() should return only valid hyperparameters.
        if exited_reason == searcher.ExitedReason.INVALID_HP:
            ops: List[searcher.Operation] = []

            self.asha_search_state.early_exit_trials.add(request_id)
            ops.append(searcher.Close(request_id))
            self.asha_search_state.closed_trials.add(request_id)
            self.asha_search_state.invalid_trials += 1

            highest_rung_index = self.asha_search_state.trial_rungs[request_id]
            rung = self.asha_search_state.rungs[highest_rung_index]
            rung.outstanding_trials -= 1

            for rung_idx in range(0, highest_rung_index + 1):
                rung = self.asha_search_state.rungs[rung_idx]
                rung.metrics = list(filter(lambda x: x.request_id != request_id, rung.metrics))

            create = searcher.Create(
                request_id=uuid.uuid4(),
                hparams=self.search_space(),
                checkpoint=None,
            )
            ops.append(create)
            ops.append(
                searcher.ValidateAfter(
                    request_id=create.request_id,
                    length=self.asha_search_state.rungs[0].units_needed,
                )
            )

            self.asha_search_state.trial_rungs[create.request_id] = 0
            self.asha_search_state.pending_trials += 1

            return ops

        self.asha_search_state.early_exit_trials.add(request_id)
        self.asha_search_state.closed_trials.add(request_id)
        return self._promote_async(request_id, sys.float_info.max)

    ############################################################################
    # Invoked when the master is asking for the progress of your search method.
    # Progress is a float number in range [0.0, 1.0], where 1.0 means that
    # your search methods completed all the trials.
    # Note that sending progress=1.0 only updates progress in the WebUI,
    # but it does not close the experiment. Ony sending Shutdown operation can
    # close the experiment.
    # In this example, progress computation is based on the number of completed trials
    # in rung 0 to the total number of trials.
    def progress(self, _: searcher.SearcherState) -> float:
        if 0 < self.asha_search_state.max_concurrent_trials < self.asha_search_state.pending_trials:
            raise RuntimeError("Pending trial is greater than max concurrent trials")
        all_trials = len(self.asha_search_state.rungs[0].metrics)

        progress = all_trials / (1.2 * self.asha_search_state.max_trials)
        if all_trials == self.asha_search_state.max_trials:
            num_valid_trials = (
                self.asha_search_state.completed_trials - self.asha_search_state.invalid_trials
            )
            progress_no_overhead = num_valid_trials / self.asha_search_state.max_trials
            progress = max(progress_no_overhead, progress)

        return progress

    ############################################################################
    # User-defined method for saving information related to the current state of
    # the search. This method is invoked by SearchRunner to save SearchMethod
    # state to support fault tolerance.
    # In this example, all information related to ASHA are stored in ASHASearchMethodState,
    # so we use pickle.dump to save the asha_search_state object.
    #
    # Note: if you have multiple objects/models/variables that are used by your
    # search method, they need to be saved in this method to enable fault tolerance.
    def save_method_state(self, path: Path) -> None:
        checkpoint_path = path.joinpath("method_state")
        with checkpoint_path.open("wb") as f:
            pickle.dump(self.asha_search_state, f)

    ############################################################################
    # User-defined method for loading SearchMethod related information.
    # This method is invoked by SearchRunner when resuming the search after
    # the search process was terminated or interrupted.
    # In this example, we simply load ASHASearchMethodState object that was
    # previously saved in save_method_state().
    #
    # Note: to ensure that your search method is initialized correctly on
    # resumption, load all objects/models/variables that were saved in
    # save_method_state().
    def load_method_state(self, path: Path) -> None:
        checkpoint_path = path.joinpath("method_state")
        with checkpoint_path.open("rb") as f:
            self.asha_search_state = pickle.load(f)

    ############################################################################
    # ASHA internal implementation details. You can skip this part.
    # If you want to learn more about ASHA, see
    # https://docs.determined.ai/latest/training/hyperparameter/search-methods/hp-adaptive-asha.html
    def _get_max_concurrent_trials(self):
        if self.asha_search_state.max_concurrent_trials > 0:
            max_concurrent_trials = min(
                self.asha_search_state.max_concurrent_trials,
                self.asha_search_state.max_trials,
            )
        else:
            max_concurrent_trials = max(
                1,
                min(
                    int(
                        pow(
                            self.asha_search_state.divisor,
                            self.asha_search_state.num_rungs - 1,
                        )
                    ),
                    self.asha_search_state.max_trials,
                ),
            )
        return max_concurrent_trials

    def _promote_async(self, request_id: uuid.UUID, metric: float) -> List[searcher.Operation]:
        rung_idx = self.asha_search_state.trial_rungs[request_id]
        rung = self.asha_search_state.rungs[rung_idx]
        rung.outstanding_trials -= 1
        added_train_workload = False

        ops: List[searcher.Operation] = []

        if rung_idx == self.asha_search_state.num_rungs - 1:
            rung.metrics.append(TrialMetric(request_id=request_id, metric=metric))

            if request_id not in self.asha_search_state.early_exit_trials:
                ops.append(searcher.Close(request_id=request_id))
                logging.info(f"Closing trial {request_id}")
                self.asha_search_state.closed_trials.add(request_id)
        else:
            next_rung = self.asha_search_state.rungs[rung_idx + 1]
            logging.info(f"Promoting in rung {rung_idx}")
            for promoted_request_id in rung.promotions_async(
                request_id, metric, self.asha_search_state.divisor
            ):
                self.asha_search_state.trial_rungs[promoted_request_id] = rung_idx + 1
                next_rung.outstanding_trials += 1
                if promoted_request_id not in self.asha_search_state.early_exit_trials:
                    logging.info(f"Promoted {promoted_request_id}")
                    units_needed = max(next_rung.units_needed - rung.units_needed, 1)
                    ops.append(searcher.ValidateAfter(promoted_request_id, units_needed))
                    added_train_workload = True
                    self.asha_search_state.pending_trials += 1
                else:
                    return self._promote_async(promoted_request_id, sys.float_info.max)

        all_trials = len(self.asha_search_state.trial_rungs) - self.asha_search_state.invalid_trials
        if not added_train_workload and all_trials < self.asha_search_state.max_trials:
            logging.info("Creating new trial instead of promoting")
            self.asha_search_state.pending_trials += 1

            create = searcher.Create(
                request_id=uuid.uuid4(),
                hparams=self.search_space(),
                checkpoint=None,
            )
            ops.append(create)
            ops.append(
                searcher.ValidateAfter(
                    request_id=create.request_id,
                    length=self.asha_search_state.rungs[0].units_needed,
                )
            )
            self.asha_search_state.trial_rungs[create.request_id] = 0

        if len(self.asha_search_state.rungs[0].metrics) == self.asha_search_state.max_trials:
            ops.extend(self._get_close_rungs_ops())

        return ops

    def _get_close_rungs_ops(self) -> List[searcher.Operation]:
        ops: List[searcher.Operation] = []

        for rung in self.asha_search_state.rungs:
            if rung.outstanding_trials > 0:
                break
            for trial_metric in rung.metrics:
                if (
                    not trial_metric.promoted
                    and trial_metric.request_id not in self.asha_search_state.closed_trials
                ):
                    if trial_metric.request_id not in self.asha_search_state.early_exit_trials:
                        logging.info(f"Closing trial {trial_metric.request_id}")
                        ops.append(searcher.Close(trial_metric.request_id))
                        self.asha_search_state.closed_trials.add(trial_metric.request_id)
        return ops


############################################################################
# To ease the process of saving and loading SearchMethod, in this example
# we encapsulate ASHA-related variables in a single object corresponding to the
# state of the search, called ASHASearchMethodState.
# ASHASearchMethodState includes search parameters (e.g., max_trials and max_rungs),
# necessary data structures (e.g., rungs and trial_rungs), and other variables
# related to the state of the search (e.g., pending trials).
class ASHASearchMethodState:
    def __init__(
        self,
        max_length: int,
        max_trials: int,
        num_rungs: int,
        divisor: int,
        max_concurrent_trials: int = 0,
    ) -> None:
        # ASHA params
        self.max_length = max_length
        self.max_trials = max_trials
        self.num_rungs = num_rungs
        self.divisor = divisor
        self.max_concurrent_trials = max_concurrent_trials
        self.is_smaller_better = True

        # structs
        self.rungs: List[Rung] = []
        self.trial_rungs: Dict[uuid.UUID, int] = {}

        # accounting variables
        self.pending_trials: int = 0
        self.completed_trials: int = 0
        self.invalid_trials: int = 0
        self.early_exit_trials: Set[uuid.UUID] = set()
        self.closed_trials: Set[uuid.UUID] = set()

        self._init_rungs()

    def _init_rungs(self) -> None:
        units_needed = 0
        for idx in range(self.num_rungs):
            downsampling_rate = pow(self.divisor, float(self.num_rungs - idx - 1))
            units_needed += max(int(self.max_length / downsampling_rate), 1)
            self.rungs.append(Rung(units_needed, idx))


############################################################################
# Helper classes that are part of the ASHASearchMethodState.
# You can skip this part.
@dataclasses.dataclass
class TrialMetric:
    request_id: uuid.UUID
    metric: float
    promoted: bool = False


@dataclasses.dataclass
class Rung:
    units_needed: int
    idx: int
    metrics: List[TrialMetric] = dataclasses.field(default_factory=list)
    outstanding_trials: int = 0

    def promotions_async(
        self, request_id: uuid.UUID, metric: float, divisor: int
    ) -> List[uuid.UUID]:
        logging.info(f"Rung {self.idx}")

        old_num_promote = len(self.metrics) // divisor
        num_promote = (len(self.metrics) + 1) // divisor

        index = self._search_metric_index(metric)
        promote_now = index < num_promote
        trial_metric = TrialMetric(request_id=request_id, metric=metric, promoted=promote_now)
        self.metrics.insert(index, trial_metric)

        if promote_now:
            return [request_id]
        if num_promote != old_num_promote and not self.metrics[old_num_promote].promoted:
            self.metrics[old_num_promote].promoted = True
            return [self.metrics[old_num_promote].request_id]

        logging.info("No promotion")
        return []

    def _search_metric_index(self, metric: float) -> int:
        i: int = 0
        j: int = len(self.metrics)
        while i < j:
            mid = (i + j) >> 1
            if self.metrics[mid].metric <= metric:
                i = mid + 1
            else:
                j = mid
        return i
