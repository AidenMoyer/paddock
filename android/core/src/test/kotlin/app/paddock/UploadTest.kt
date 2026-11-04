package app.paddock

import app.paddock.net.MediaUris
import app.paddock.net.UploadStatus
import app.paddock.net.UploadStatus.Outcome
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class UploadTest {
    private val picked = "content://media/picker/0/com.android.providers.media.photopicker/media/1000012345"

    @Test fun onePickedPhotoMakesOneBatch() {
        assertEquals(listOf(listOf(picked)), MediaUris.batches(listOf(picked)))
    }

    @Test fun nothingPickedMakesNoBatches() {
        assertEquals(emptyList(), MediaUris.batches(emptyList()))
    }

    @Test fun aFullPhotoPickerSelectionFitsInOneBatch() {
        val fifty = (1..50).map { "$picked$it" }
        assertEquals(listOf(fifty), MediaUris.batches(fifty))
    }

    @Test fun aHugeSelectionIsSplitWithoutLosingOrReorderingAnything() {
        val many = (1..2000).map { "content://com.android.providers.media.documents/document/image%3A$it" }
        val batches = MediaUris.batches(many)
        assertTrue(batches.size > 1)
        assertEquals(many, batches.flatten())
        assertTrue(batches.none { it.isEmpty() })
        // a worker's input has to stay under 10 KB, with room left for what WorkManager adds per string
        assertTrue(batches.all { b -> b.sumOf { it.length + 8 } <= 8000 }, "a batch went over budget")
    }

    @Test fun anAddressLongerThanTheWholeBudgetGetsABatchOfItsOwn() {
        val long = "content://x/" + "a".repeat(9000)
        val batches = MediaUris.batches(listOf(picked, long, picked))
        assertEquals(listOf(picked, long, picked), batches.flatten())
        assertEquals(listOf(long), batches[1])
    }

    private fun state() = assertNotNull(UploadStatus.state.value)

    @BeforeTest fun startClean() = UploadStatus.dismiss()

    @Test fun nothingToReportBeforeAnythingIsPicked() {
        assertNull(UploadStatus.state.value)
    }

    @Test fun aBatchGoesFromWaitingToFinished() {
        UploadStatus.queue(listOf("a", "b"))
        assertTrue(state().running)
        assertEquals(0f, state().fraction)
        assertEquals(listOf("a", "b"), UploadStatus.remaining(listOf("a", "b")))

        UploadStatus.working("a.heic", 0.5f)
        assertEquals("a.heic", state().current)
        assertEquals(0.25f, state().fraction)

        UploadStatus.finished("a", Outcome.Uploaded)
        assertEquals(1, state().finished)
        assertNull(state().current)
        assertTrue(state().running)

        UploadStatus.finished("b", Outcome.Failed)
        assertFalse(state().running)
        assertEquals(1, state().uploaded)
        assertEquals(1, state().failed)
        assertEquals(1f, state().fraction)
    }

    @Test fun whatsOnScreenRefreshesOnceWhenABatchEnds() {
        val before = UploadStatus.landed.value
        UploadStatus.queue(listOf("a", "b"))
        UploadStatus.finished("a", Outcome.Uploaded)
        assertEquals(before, UploadStatus.landed.value)     // not after every file of a big batch
        UploadStatus.finished("b", Outcome.Already)
        assertEquals(before + 1, UploadStatus.landed.value)

        UploadStatus.queue(listOf("c"))
        UploadStatus.finished("c", Outcome.Already)         // it may have just joined a project
        assertEquals(before + 2, UploadStatus.landed.value)

        UploadStatus.queue(listOf("d"))
        UploadStatus.finished("d", Outcome.Failed)          // nothing new to show
        assertEquals(before + 2, UploadStatus.landed.value)
    }

    @Test fun aRetriedWorkerSkipsWhatAlreadyFinished() {
        UploadStatus.queue(listOf("a", "b"))
        UploadStatus.finished("a", Outcome.Uploaded)
        UploadStatus.waiting("The connection dropped")
        assertEquals("The connection dropped", state().waiting)
        assertTrue(state().running)

        assertEquals(listOf("b"), UploadStatus.remaining(listOf("a", "b")))
        UploadStatus.working("b.heic", 0f)
        assertNull(state().waiting)
    }

    @Test fun aWorkerThatOutlivedTheAppPicksItsListBackUp() {
        assertEquals(listOf("a", "b"), UploadStatus.remaining(listOf("a", "b")))
        assertEquals(2, state().total)
        assertTrue(state().running)
    }

    @Test fun tryingAgainStartsAFailedFileOver() {
        UploadStatus.queue(listOf("a"))
        UploadStatus.finished("a", Outcome.Failed)
        assertFalse(state().running)

        UploadStatus.queue(listOf("a"))
        assertTrue(state().running)
        assertEquals(0, state().failed)
        assertEquals(1, state().total)
    }

    @Test fun aNewPickAfterABatchEndedStartsCountingFromZero() {
        UploadStatus.queue(listOf("a", "b"))
        UploadStatus.finished("a", Outcome.Uploaded)
        UploadStatus.finished("b", Outcome.Uploaded)

        UploadStatus.queue(listOf("c"))
        assertEquals(1, state().total)
        assertEquals(0, state().finished)
    }

    @Test fun pickingMoreWhileOneIsRunningAddsToIt() {
        UploadStatus.queue(listOf("a"))
        UploadStatus.queue(listOf("b"))
        assertEquals(2, state().total)
    }

    @Test fun cancellingClearsTheBannerAndALateResultCantBringItBack() {
        UploadStatus.queue(listOf("a"))
        UploadStatus.dismiss()
        assertNull(UploadStatus.state.value)
        UploadStatus.working("a.heic", 0.5f)
        UploadStatus.finished("a", Outcome.Uploaded)
        assertNull(UploadStatus.state.value)
    }

    @Test fun progressStaysInRangeWhateverTheServerSays() {
        UploadStatus.queue(listOf("a"))
        UploadStatus.working("a", 7f)
        assertEquals(1f, state().fraction)
        UploadStatus.working("a", -3f)
        assertEquals(0f, state().fraction)
    }
}
